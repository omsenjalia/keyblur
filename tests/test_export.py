import os

import numpy as np
import pytest

from keyblur import export, video
from keyblur.model import Keyframe, Project, Track


def _frames(path, fps, n):
    r = video.FrameReader(path, fps)
    out = [f for _, f in r.iter_frames(0, n)]
    r.close()
    return out


@pytest.mark.parametrize("fixture,square", [("mp4_clip", False), ("single_vob", True)])
def test_export_blurs_region_only(request, tmp_path, fixture, square):
    src_path = request.getfixturevalue(fixture)
    src, playable = video.prepare_source(src_path)
    track = Track(name="t", keyframes=[
        Keyframe(time=0.0, x=0.3, y=0.5, radius_x=0.15, radius_y=0.2, strength=10, interpolation="linear"),
        Keyframe(time=src.duration_sec, x=0.7, y=0.5, radius_x=0.15, radius_y=0.2, strength=10),
    ])
    proj = Project(source=src, tracks=[track])
    out = str(tmp_path / "out.mp4")
    settings = dict(proj.export_settings, crf=12, preset="ultrafast", square_pixels=square,
                    deinterlace=False)
    calls = []
    export.render(proj, playable, out, settings, progress=lambda n, t: calls.append((n, t)))
    assert os.path.isfile(out)
    assert calls and calls[-1][0] == calls[-1][1] == src.frame_count

    info = video.probe(out)
    assert info["has_audio"]
    assert info["fps"] == src.fps
    n_out = int(round(info["duration"] * float(info["fps"])))
    assert abs(n_out - src.frame_count) <= 1
    if square:
        assert (info["width"], info["height"]) == (640, 480)
    else:
        assert (info["width"], info["height"]) == (src.width, src.height)

    # first output frame should match the locally blurred source frame, not the original
    import cv2
    from keyblur.blur import apply_blurs
    got = _frames(out, info["fps"], 1)[0].astype(int)
    ref = _frames(playable, src.fps, 1)[0]
    expected = apply_blurs(ref, proj.states_at(0.0), 1.0)
    if square:
        ref = cv2.resize(ref, (640, 480), interpolation=cv2.INTER_AREA)
        expected = cv2.resize(expected, (640, 480), interpolation=cv2.INTER_AREA)
    h, w = got.shape[:2]
    region = (slice(int(h * 0.4), int(h * 0.6)), slice(int(w * 0.22), int(w * 0.38)))
    to_expected = np.abs(got[region] - expected[region].astype(int)).mean()
    to_original = np.abs(got[region] - ref[region].astype(int)).mean()
    assert to_expected < 3 and to_original > 2 * to_expected, (to_expected, to_original)
    far = (slice(int(h * 0.4), int(h * 0.6)), slice(int(w * 0.7), int(w * 0.8)))
    assert np.abs(got[far] - ref[far].astype(int)).mean() < 3


def test_cancel_removes_partial(tmp_path, mp4_clip):
    src, playable = video.prepare_source(mp4_clip)
    proj = Project(source=src)
    out = str(tmp_path / "c.mp4")
    with pytest.raises(export.ExportCancelled):
        export.render(proj, playable, out, dict(proj.export_settings, preset="ultrafast"),
                      cancelled=lambda: True)
    assert not os.path.exists(out)
