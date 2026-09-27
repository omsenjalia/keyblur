import os

import numpy as np
import pytest

from keyblur import video


def _diff(a, b):
    return np.abs(a.astype(int) - b.astype(int)).mean()


def _sequential(path, fps, n):
    r = video.FrameReader(path, fps)
    frames = [f for _, f in r.iter_frames(0, n)]
    r.close()
    return frames


def test_probe_vob(single_vob):
    info = video.probe(single_vob)
    assert (info["width"], info["height"]) == (720, 480)
    assert abs(float(info["fps"]) - 29.97) < 0.01
    assert info["has_audio"]


def test_prepare_single_vob(single_vob):
    src, playable = video.prepare_source(single_vob)
    assert src.type == "vob_single"
    assert playable.endswith(".mkv") and os.path.isfile(playable)
    assert abs(src.duration_sec - 4.0) < 0.2
    assert src.display_aspect == pytest.approx(4 / 3, rel=0.01)
    assert video.proxy_size(src) == (640, 480)


@pytest.mark.parametrize("fixture", ["single_vob", "mp4_clip"])
def test_frame_accurate_random_access(request, fixture):
    path = request.getfixturevalue(fixture)
    src, playable = video.prepare_source(path)
    n = src.frame_count
    ref = _sequential(playable, src.fps, n)
    assert len(ref) == n
    r = video.FrameReader(playable, src.fps)
    for idx in [n - 1, 0, 45, 44, 17, 60, 3, n // 2, 1]:
        idx = min(idx, n - 1)
        got = r.frame_at(idx)
        assert _diff(got, ref[idx]) < 0.5, f"frame {idx} mismatch"
        # and it really differs from its neighbour (testsrc2 animates)
        other = ref[idx + 1] if idx + 1 < n else ref[idx - 1]
        assert _diff(got, other) > 0.5
    r.close()


def test_video_ts_title_detection(video_ts):
    folder, _ = video_ts
    sets = video.find_title_sets(folder)
    assert set(sets) == {"01", "02"}
    assert [os.path.basename(p) for p in sets["01"]] == ["VTS_01_1.VOB", "VTS_01_2.VOB"]
    assert [os.path.basename(p) for p in video.main_title_set(folder)] == ["VTS_01_1.VOB", "VTS_01_2.VOB"]


def test_video_ts_concat_matches_original(video_ts):
    folder, full = video_ts
    src, playable = video.prepare_source(folder)
    ref_src, ref_playable = video.prepare_source(full)
    assert src.type == "vob_video_ts"
    assert abs(src.duration_sec - ref_src.duration_sec) < 0.1
    assert abs(src.duration_sec - 6.0) < 0.2
    a = video.FrameReader(playable, src.fps)
    b = video.FrameReader(ref_playable, ref_src.fps)
    for idx in (0, 89, 90, 91, 150):
        assert _diff(a.frame_at(idx), b.frame_at(idx)) < 0.5
    a.close()
    b.close()


def test_proxy_output_size(single_vob):
    src, playable = video.prepare_source(single_vob)
    r = video.FrameReader(playable, src.fps, out_size=video.proxy_size(src))
    assert r.frame_at(10).shape == (480, 640, 3)
    r.close()
