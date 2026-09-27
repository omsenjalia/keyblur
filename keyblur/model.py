"""Project data model: tracks, keyframes, interpolation and .keyblur JSON I/O.

Pure Python (no Qt) so it can be unit-tested in isolation.
"""
from __future__ import annotations

import copy
import json
import os
import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

SCHEMA_VERSION = 1
INTERPOLATIONS = ("hold", "linear", "ease")
TIME_EPS = 1e-4  # well under half a frame at any realistic fps

DEFAULT_EXPORT_SETTINGS = {
    "output_path": "",
    "codec": "libx264",
    "crf": 18,
    "preset": "medium",
    "container": "mp4",
    "deinterlace": None,  # None = auto (on when the source is interlaced)
    "square_pixels": False,
}


def normalize_fps(fps: float) -> Fraction:
    """Map a float frame rate to an exact rational (29.97 -> 30000/1001)."""
    for num in (24000, 30000, 48000, 60000, 120000):
        if abs(fps - num / 1001) < 0.005:
            return Fraction(num, 1001)
    return Fraction(fps).limit_denominator(1001)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class Keyframe:
    time: float
    x: float = 0.5
    y: float = 0.5
    radius_x: float = 0.08
    radius_y: float = 0.08
    strength: float = 25.0
    interpolation: str = "linear"
    id: str = field(default_factory=lambda: _new_id("kf"), compare=False)

    PROPS = ("time", "x", "y", "radius_x", "radius_y", "strength", "interpolation")

    def to_dict(self) -> dict:
        return {p: getattr(self, p) for p in self.PROPS}

    @classmethod
    def from_dict(cls, d: dict) -> "Keyframe":
        interp = d.get("interpolation", "linear")
        if interp not in INTERPOLATIONS:
            interp = "linear"
        return cls(
            time=float(d["time"]),
            x=float(d.get("x", 0.5)),
            y=float(d.get("y", 0.5)),
            radius_x=float(d.get("radius_x", 0.08)),
            radius_y=float(d.get("radius_y", d.get("radius_x", 0.08))),
            strength=float(d.get("strength", 25)),
            interpolation=interp,
        )


@dataclass
class BlurState:
    """Evaluated (interpolated) blur parameters for one track at one instant."""
    x: float
    y: float
    radius_x: float
    radius_y: float
    strength: float
    track_id: str = ""


def _ease(u: float) -> float:
    return u * u * (3.0 - 2.0 * u)


def _lerp(a: float, b: float, u: float) -> float:
    return a + (b - a) * u


@dataclass
class Track:
    name: str = "Track"
    visible: bool = True
    sticky: bool = False
    lock_aspect: bool = True
    keyframes: list[Keyframe] = field(default_factory=list)
    id: str = field(default_factory=lambda: _new_id("track"))

    def sort(self) -> None:
        self.keyframes.sort(key=lambda k: k.time)

    def keyframe_by_id(self, kf_id: str) -> Optional[Keyframe]:
        return next((k for k in self.keyframes if k.id == kf_id), None)

    def keyframe_at(self, t: float) -> Optional[Keyframe]:
        return next((k for k in self.keyframes if abs(k.time - t) < TIME_EPS), None)

    def active_range(self, duration: float) -> Optional[tuple[float, float]]:
        if not self.keyframes:
            return None
        start = self.keyframes[0].time
        end = duration if self.sticky else self.keyframes[-1].time
        return start, max(start, end)

    def state_at(self, t: float) -> Optional[BlurState]:
        kfs = self.keyframes
        if not kfs or t < kfs[0].time - TIME_EPS:
            return None
        last = kfs[-1]
        if t >= last.time - TIME_EPS:
            if self.sticky or abs(t - last.time) < TIME_EPS:
                return self._state(last)
            return None
        # find segment a <= t < b
        for a, b in zip(kfs, kfs[1:]):
            if a.time - TIME_EPS <= t < b.time - TIME_EPS:
                span = b.time - a.time
                if a.interpolation == "hold" or span <= 0:
                    return self._state(a)
                u = min(1.0, max(0.0, (t - a.time) / span))
                if a.interpolation == "ease":
                    u = _ease(u)
                return BlurState(
                    x=_lerp(a.x, b.x, u),
                    y=_lerp(a.y, b.y, u),
                    radius_x=_lerp(a.radius_x, b.radius_x, u),
                    radius_y=_lerp(a.radius_y, b.radius_y, u),
                    strength=_lerp(a.strength, b.strength, u),
                    track_id=self.id,
                )
        return None

    def _state(self, k: Keyframe) -> BlurState:
        return BlurState(k.x, k.y, k.radius_x, k.radius_y, k.strength, self.id)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "visible": self.visible,
            "sticky": self.sticky,
            "lock_aspect": self.lock_aspect,
            "keyframes": [k.to_dict() for k in self.keyframes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        t = cls(
            name=str(d.get("name", "Track")),
            visible=bool(d.get("visible", True)),
            sticky=bool(d.get("sticky", False)),
            lock_aspect=bool(d.get("lock_aspect", True)),
            keyframes=[Keyframe.from_dict(k) for k in d.get("keyframes", [])],
            id=str(d.get("id") or _new_id("track")),
        )
        t.sort()
        return t


@dataclass
class SourceInfo:
    path: str  # absolute at runtime
    type: str = "generic"  # vob_single | vob_video_ts | generic
    fps: Fraction = Fraction(30000, 1001)
    width: int = 0
    height: int = 0
    duration_sec: float = 0.0
    sar: Fraction = Fraction(1, 1)
    interlaced: bool = False
    title_set: Optional[str] = None  # VIDEO_TS title set number, e.g. "01"

    @property
    def frame_count(self) -> int:
        return max(1, int(round(self.duration_sec * float(self.fps))))

    @property
    def display_aspect(self) -> float:
        """Display width / height, accounting for non-square pixels."""
        if not self.height:
            return 16 / 9
        return self.width * float(self.sar) / self.height

    def to_dict(self, project_dir: Optional[str]) -> dict:
        path = self.path
        if project_dir:
            try:
                path = os.path.relpath(self.path, project_dir)
            except ValueError:  # different drive on Windows
                path = self.path
        return {
            "path": path.replace("\\", "/"),
            "type": self.type,
            "fps": round(float(self.fps), 6),
            "fps_rational": f"{self.fps.numerator}/{self.fps.denominator}",
            "width": self.width,
            "height": self.height,
            "duration_sec": self.duration_sec,
            "sar": f"{self.sar.numerator}/{self.sar.denominator}",
            "interlaced": self.interlaced,
            "title_set": self.title_set,
        }

    @classmethod
    def from_dict(cls, d: dict, project_dir: Optional[str]) -> "SourceInfo":
        path = d.get("path", "")
        if path and not os.path.isabs(path) and project_dir:
            path = os.path.normpath(os.path.join(project_dir, path))
        if d.get("fps_rational"):
            fps = Fraction(d["fps_rational"])
        else:
            fps = normalize_fps(float(d.get("fps", 29.97)))
        return cls(
            path=path,
            type=d.get("type", "generic"),
            fps=fps,
            width=int(d.get("width", 0)),
            height=int(d.get("height", 0)),
            duration_sec=float(d.get("duration_sec", 0.0)),
            sar=Fraction(d.get("sar", "1/1")),
            interlaced=bool(d.get("interlaced", False)),
            title_set=d.get("title_set"),
        )


class ProjectError(Exception):
    pass


@dataclass
class Project:
    source: Optional[SourceInfo] = None
    tracks: list[Track] = field(default_factory=list)
    export_settings: dict = field(default_factory=lambda: dict(DEFAULT_EXPORT_SETTINGS))

    # ---- time helpers -------------------------------------------------
    @property
    def fps(self) -> Fraction:
        return self.source.fps if self.source else Fraction(30000, 1001)

    @property
    def duration(self) -> float:
        return self.source.duration_sec if self.source else 0.0

    def frame_of(self, t: float) -> int:
        return int(round(t * float(self.fps)))

    def time_of(self, frame: int) -> float:
        return float(Fraction(frame) / self.fps)

    def snap(self, t: float) -> float:
        t = max(0.0, min(t, self.duration)) if self.duration else max(0.0, t)
        return self.time_of(self.frame_of(t))

    # ---- lookup ---------------------------------------------------------
    def track_by_id(self, track_id: str) -> Optional[Track]:
        return next((t for t in self.tracks if t.id == track_id), None)

    def track_index(self, track_id: str) -> int:
        for i, t in enumerate(self.tracks):
            if t.id == track_id:
                return i
        return -1

    def states_at(self, t: float) -> list[BlurState]:
        out = []
        for tr in self.tracks:
            if tr.visible:
                s = tr.state_at(t)
                if s is not None:
                    out.append(s)
        return out

    def next_track_name(self) -> str:
        names = {t.name for t in self.tracks}
        i = len(self.tracks) + 1
        while f"Track {i}" in names:
            i += 1
        return f"Track {i}"

    # ---- serialization ---------------------------------------------------
    def to_dict(self, project_path: Optional[str] = None) -> dict:
        project_dir = os.path.dirname(os.path.abspath(project_path)) if project_path else None
        return {
            "version": SCHEMA_VERSION,
            "source_video": self.source.to_dict(project_dir) if self.source else None,
            "tracks": [t.to_dict() for t in self.tracks],
            "export_settings": copy.deepcopy(self.export_settings),
        }

    @classmethod
    def from_dict(cls, d: dict, project_path: Optional[str] = None) -> "Project":
        if not isinstance(d, dict):
            raise ProjectError("Project file root must be a JSON object.")
        version = d.get("version", 1)
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise ProjectError(f"Unsupported project version: {version!r}")
        project_dir = os.path.dirname(os.path.abspath(project_path)) if project_path else None
        src = d.get("source_video")
        settings = dict(DEFAULT_EXPORT_SETTINGS)
        settings.update(d.get("export_settings") or {})
        try:
            return cls(
                source=SourceInfo.from_dict(src, project_dir) if src else None,
                tracks=[Track.from_dict(t) for t in d.get("tracks", [])],
                export_settings=settings,
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            raise ProjectError(f"Malformed project file: {e}") from e

    def save(self, path: str) -> None:
        data = self.to_dict(path)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Project":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ProjectError(f"Not a valid JSON file: {e}") from e
        return cls.from_dict(data, path)
