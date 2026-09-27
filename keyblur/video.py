"""Video source handling: probing, VOB / VIDEO_TS preparation, frame-accurate reading."""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections import OrderedDict
from fractions import Fraction
from typing import Callable, Iterator, Optional

import av
import numpy as np

from .model import SourceInfo

PROXY_MAX_W = 960
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class VideoError(Exception):
    pass


# --------------------------------------------------------------------------
# ffmpeg discovery
# --------------------------------------------------------------------------
_tool_overrides: dict[str, str] = {}


def set_tool_path(name: str, path: str) -> None:
    _tool_overrides[name] = path


def tool(name: str) -> str:
    """Return the path to ffmpeg/ffprobe or raise VideoError."""
    if name in _tool_overrides and os.path.isfile(_tool_overrides[name]):
        return _tool_overrides[name]
    found = shutil.which(name)
    if not found:
        raise VideoError(f"'{name}' was not found on PATH. Install ffmpeg or set its location.")
    return found


def run_tool(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, creationflags=_NO_WINDOW, **kw)


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------
def _parse_ratio(s: Optional[str]) -> Optional[Fraction]:
    if not s or s in ("0/0", "N/A"):
        return None
    try:
        f = Fraction(s.replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def _parse_hms(s: Optional[str]) -> Optional[float]:
    """Parse Matroska 'HH:MM:SS.nnnnnnnnn' duration tags."""
    if not s:
        return None
    try:
        h, m, sec = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except ValueError:
        return None


def probe(path: str) -> dict:
    """Probe a media file with ffprobe; returns a dict of video parameters."""
    args = [tool("ffprobe"), "-v", "error", "-analyzeduration", "100M", "-probesize", "100M",
            "-print_format", "json", "-show_format", "-show_streams", path]
    res = run_tool(args)
    if res.returncode != 0:
        raise VideoError(f"ffprobe failed on {path}:\n{res.stderr.decode(errors='replace')}")
    data = json.loads(res.stdout.decode("utf-8", errors="replace"))
    vstreams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"
                and not s.get("disposition", {}).get("attached_pic")]
    if not vstreams:
        raise VideoError(f"No video stream found in {path}")
    v = vstreams[0]
    r = _parse_ratio(v.get("r_frame_rate"))
    avg = _parse_ratio(v.get("avg_frame_rate"))
    fps = r or avg or Fraction(30000, 1001)
    if r and avg and r > avg * Fraction(3, 2):  # field-rate r_frame_rate on interlaced sources
        fps = avg
    # prefer the *video* stream's duration: container duration includes audio tails
    duration = None
    candidates = [v.get("duration"), _parse_hms((v.get("tags") or {}).get("DURATION")),
                  data.get("format", {}).get("duration")]
    for c in candidates:
        try:
            if c is not None and float(c) > 0:
                duration = float(c)
                break
        except (ValueError, TypeError):
            pass
    try:
        start = float(v.get("start_time") or 0.0)
    except ValueError:
        start = 0.0
    if duration is not None and 0 < start < duration:
        duration -= start  # end timestamp -> length (B-frame delay offsets start)
    if duration is None and v.get("nb_frames"):
        duration = int(v["nb_frames"]) / float(fps)
    return {
        "fps": fps,
        "width": int(v["width"]),
        "height": int(v["height"]),
        "sar": _parse_ratio(v.get("sample_aspect_ratio")) or Fraction(1),
        "duration": duration or 0.0,
        "interlaced": v.get("field_order") in ("tt", "bb", "tb", "bt"),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


# --------------------------------------------------------------------------
# VOB / VIDEO_TS
# --------------------------------------------------------------------------
_VTS_RE = re.compile(r"^VTS_(\d\d)_(\d)\.VOB$", re.IGNORECASE)


def find_title_sets(folder: str) -> dict[str, list[str]]:
    """Map title-set number -> ordered list of content VOBs (menu VOB _0 excluded)."""
    if os.path.isdir(os.path.join(folder, "VIDEO_TS")):
        folder = os.path.join(folder, "VIDEO_TS")
    sets: dict[str, list[tuple[int, str]]] = {}
    for f in os.listdir(folder):
        m = _VTS_RE.match(f)
        if m and m.group(2) != "0":
            sets.setdefault(m.group(1), []).append((int(m.group(2)), os.path.join(folder, f)))
    return {k: [p for _, p in sorted(v)] for k, v in sorted(sets.items())}


def title_set_size(files: list[str]) -> int:
    return sum(os.path.getsize(f) for f in files)


def main_title_set(folder: str) -> list[str]:
    sets = find_title_sets(folder)
    if not sets:
        raise VideoError(f"No VTS_xx_N.VOB files found in {folder}")
    return max(sets.values(), key=title_set_size)


def source_type_for(path: str) -> str:
    if os.path.isdir(path):
        return "vob_video_ts"
    if path.lower().endswith(".vob"):
        return "vob_single"
    return "generic"


def vob_files_for(path: str, title_set: Optional[str] = None) -> list[str]:
    """VOB file list for a single .vob path or a VIDEO_TS folder."""
    if os.path.isdir(path):
        if title_set:
            sets = find_title_sets(path)
            if title_set not in sets:
                raise VideoError(f"Title set {title_set} not found in {path}")
            return sets[title_set]
        return main_title_set(path)
    return [path]


def cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    d = os.path.join(base, "KeyBlur", "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_key(files: list[str]) -> str:
    h = hashlib.sha1()
    for f in files:
        st = os.stat(f)
        h.update(f"{os.path.abspath(f)}|{st.st_size}|{int(st.st_mtime)}\n".encode())
    return h.hexdigest()[:16]


def remux_vobs(files: list[str], out_dir: Optional[str] = None,
               progress: Optional[Callable[[float], None]] = None) -> str:
    """Losslessly remux MPEG-PS VOB file(s) into a cached .mkv with clean timestamps."""
    out_dir = out_dir or cache_dir()
    out = os.path.join(out_dir, f"vob_{_cache_key(files)}.mkv")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    total = title_set_size(files) or 1
    concat = "concat:" + "|".join(files) if len(files) > 1 else files[0]
    tmp = out + ".part.mkv"
    args = [tool("ffmpeg"), "-hide_banner", "-y", "-nostdin",
            "-analyzeduration", "100M", "-probesize", "100M",
            "-fflags", "+genpts+discardcorrupt", "-i", concat,
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-progress", "pipe:1", "-nostats", tmp]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_NO_WINDOW)
    # progress reports cumulative output size; input size is a good enough estimate
    import threading
    err_chunks: list[bytes] = []
    t = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    t.start()
    for line in proc.stdout:
        if progress and line.startswith(b"total_size="):
            try:
                progress(min(1.0, int(line.split(b"=")[1]) / total))
            except ValueError:
                pass
    proc.wait()
    t.join()
    if proc.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise VideoError("Remuxing VOB failed:\n" + b"".join(err_chunks).decode(errors="replace")[-3000:])
    os.replace(tmp, out)
    return out


def prepare_source(path: str, title_set: Optional[str] = None,
                   progress: Optional[Callable[[float], None]] = None) -> tuple[SourceInfo, str]:
    """Return (SourceInfo, playable_path). VOB sources are remuxed to a cached MKV."""
    path = os.path.abspath(path)
    stype = source_type_for(path)
    if stype == "generic":
        if not os.path.isfile(path):
            raise VideoError(f"File not found: {path}")
        playable = path
    else:
        if stype == "vob_video_ts" and not title_set:
            sets = find_title_sets(path)
            if not sets:
                raise VideoError(f"No VTS_xx_N.VOB files found in {path}")
            title_set = max(sets, key=lambda k: title_set_size(sets[k]))
        playable = remux_vobs(vob_files_for(path, title_set), progress=progress)
    info = probe(playable)
    src = SourceInfo(path=path, type=stype, fps=info["fps"], width=info["width"],
                     height=info["height"], duration_sec=info["duration"], sar=info["sar"],
                     interlaced=info["interlaced"],
                     title_set=title_set if stype == "vob_video_ts" else None)
    return src, playable


def proxy_size(src: SourceInfo, max_w: int = PROXY_MAX_W) -> tuple[int, int]:
    """Display-aspect-correct proxy dimensions (even numbers)."""
    aspect = src.display_aspect
    disp_w = src.height * aspect
    w = min(max_w, disp_w)
    h = w / aspect
    return max(2, int(round(w / 2)) * 2), max(2, int(round(h / 2)) * 2)


# --------------------------------------------------------------------------
# frame reading
# --------------------------------------------------------------------------
class FrameReader:
    """Frame-accurate random access + sequential decoding via PyAV.

    Not thread-safe: use one instance per thread.
    """

    def __init__(self, path: str, fps: Fraction, out_size: Optional[tuple[int, int]] = None,
                 cache_size: int = 48):
        try:
            self.container = av.open(path, options={"analyzeduration": "100000000",
                                                    "probesize": "100000000"})
        except av.FFmpegError as e:
            raise VideoError(f"Could not open {path}: {e}") from e
        if not self.container.streams.video:
            raise VideoError(f"No video stream in {path}")
        self.stream = self.container.streams.video[0]
        self.stream.thread_type = "AUTO"
        self.fps = fps
        self.tb = self.stream.time_base
        self.start_pts = self.stream.start_time if self.stream.start_time is not None else 0
        self.out_size = out_size
        self._decoder: Optional[Iterator] = None
        self._last_index = -10**9
        self._last_frame: Optional[np.ndarray] = None
        self._cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self._cache_size = cache_size

    closed = False

    def close(self) -> None:
        self.closed = True
        self._decoder = None
        try:
            self.container.close()
        except Exception:
            pass

    # -- helpers --
    def _index_of(self, frame: av.VideoFrame) -> int:
        if frame.pts is None:
            return self._last_index + 1
        return int(round(float((frame.pts - self.start_pts) * self.tb * self.fps)))

    def _convert(self, frame: av.VideoFrame) -> np.ndarray:
        if self.out_size:
            w, h = self.out_size
            return frame.to_ndarray(format="bgr24", width=w, height=h)
        return frame.to_ndarray(format="bgr24")

    def _decode_next(self) -> Optional[tuple[int, av.VideoFrame]]:
        if self._decoder is None:
            self._decoder = self.container.decode(self.stream)
        try:
            f = next(self._decoder)
        except (StopIteration, av.EOFError):
            return None
        except av.FFmpegError:
            # corrupt packet: skip ahead
            return self._decode_next()
        idx = self._index_of(f)
        self._last_index = idx
        return idx, f

    def _seek(self, index: int) -> None:
        t = Fraction(index) / self.fps
        target = int(self.start_pts + t / self.tb) if self.tb else 0
        self.container.seek(max(self.start_pts, target), stream=self.stream, backward=True,
                            any_frame=False)
        self._decoder = None
        self._last_index = -10**9

    # -- public API --
    def frame_at(self, index: int) -> Optional[np.ndarray]:
        """Return the frame with the given index (nearest earlier frame if missing)."""
        if self.closed:
            return None
        index = max(0, index)
        if index in self._cache:
            self._cache.move_to_end(index)
            return self._cache[index]
        # decode forward if the target is just ahead, otherwise seek
        ahead = index - self._last_index
        if not (0 < ahead <= int(float(self.fps) * 2)):
            back = 0
            while True:
                self._seek(max(0, index - back))
                first = self._decode_next()
                if first is None:
                    return self._last_frame
                if first[0] <= index or index - back <= 0:
                    pending = first
                    break
                back = max(int(float(self.fps)), back * 2)
        else:
            pending = self._decode_next()
        prev = None
        while pending is not None:
            idx, f = pending
            if idx >= index:
                chosen = f if idx == index or prev is None else prev
                arr = self._convert(chosen)
                self._remember(index, arr)
                return arr
            prev = f
            pending = self._decode_next()
        if prev is not None:  # past end
            arr = self._convert(prev)
            self._remember(index, arr)
            return arr
        return self._last_frame

    def _remember(self, index: int, arr: np.ndarray) -> None:
        self._last_frame = arr
        self._cache[index] = arr
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def iter_frames(self, start: int = 0, count: Optional[int] = None) -> Iterator[tuple[int, np.ndarray]]:
        """Yield (index, frame) at a constant frame rate from ``start``.

        Missing frames are filled by repeating the previous one and duplicate
        timestamps are dropped, so the output stays in sync with audio.
        """
        self._seek(start)
        want = start
        last_arr: Optional[np.ndarray] = None
        end = None if count is None else start + count
        while end is None or want < end:
            nxt = self._decode_next()
            if nxt is None:
                break
            idx, f = nxt
            if idx < want:
                continue
            arr = self._convert(f)
            if last_arr is None:
                last_arr = arr
            while want < idx and last_arr is not None and (end is None or want < end):
                yield want, last_arr
                want += 1
            if end is not None and want >= end:
                break
            yield want, arr
            last_arr = arr
            want += 1
        # pad to the requested count (e.g. duration rounding)
        while end is not None and want < end and last_arr is not None:
            yield want, last_arr
            want += 1
