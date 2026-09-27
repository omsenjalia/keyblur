import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from keyblur import video  # noqa: E402


def _ffmpeg(*args):
    res = subprocess.run([video.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args],
                         capture_output=True)
    assert res.returncode == 0, res.stderr.decode(errors="replace")


def _make_vob(path, seconds, size="720x480"):
    _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30000/1001:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "mpeg2video", "-q:v", "4", "-g", "15", "-bf", "2",
            "-aspect", "4:3", "-c:a", "mp2", "-b:a", "192k", "-f", "vob", path)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def single_vob(media_dir):
    p = str(media_dir / "single.vob")
    _make_vob(p, 4)
    return p


@pytest.fixture(scope="session")
def mp4_clip(media_dir):
    p = str(media_dir / "clip.mp4")
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac", p)
    return p


@pytest.fixture(scope="session")
def video_ts(media_dir):
    """Fake DVD: one 6 s title split at a pack boundary into two VOBs, plus a menu and a short extra."""
    root = media_dir / "DVD" / "VIDEO_TS"
    root.mkdir(parents=True)
    full = str(media_dir / "full.vob")
    _make_vob(full, 6)
    data = open(full, "rb").read()
    cut = (len(data) // 2) // 2048 * 2048
    (root / "VTS_01_1.VOB").write_bytes(data[:cut])
    (root / "VTS_01_2.VOB").write_bytes(data[cut:])
    _make_vob(str(root / "VTS_01_0.VOB"), 1)   # menu, must be skipped
    _make_vob(str(root / "VTS_02_1.VOB"), 1)   # smaller extra title
    return str(root.parent), full
