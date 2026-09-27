import json
import os
from fractions import Fraction

import pytest

from keyblur.model import Keyframe, Project, ProjectError, SourceInfo, Track


def make_track(interp="linear", sticky=False):
    return Track(name="T", sticky=sticky, keyframes=[
        Keyframe(time=1.0, x=0.2, y=0.2, radius_x=0.1, radius_y=0.1, strength=10, interpolation=interp),
        Keyframe(time=3.0, x=0.6, y=0.4, radius_x=0.2, radius_y=0.3, strength=30, interpolation="linear"),
    ])


def test_before_first_and_after_last_inactive():
    t = make_track()
    assert t.state_at(0.5) is None
    assert t.state_at(3.5) is None
    assert t.state_at(3.0).x == pytest.approx(0.6)


def test_linear_midpoint():
    s = make_track("linear").state_at(2.0)
    assert (s.x, s.y, s.radius_x, s.radius_y, s.strength) == pytest.approx((0.4, 0.3, 0.15, 0.2, 20))


def test_hold_snaps():
    t = make_track("hold")
    assert t.state_at(2.99).x == pytest.approx(0.2)
    assert t.state_at(3.0).x == pytest.approx(0.6)


def test_ease_is_symmetric_and_slow_at_ends():
    t = make_track("ease")
    assert t.state_at(2.0).x == pytest.approx(0.4)
    early = t.state_at(1.2).x - 0.2
    assert early < 0.4 * 0.1  # less than linear progress (10%)


def test_sticky_holds_last_value():
    t = make_track(sticky=True)
    s = t.state_at(100.0)
    assert s is not None and s.x == pytest.approx(0.6)
    assert t.active_range(50.0) == (1.0, 50.0)


def test_single_keyframe():
    t = Track(keyframes=[Keyframe(time=2.0)])
    assert t.state_at(2.0) is not None
    assert t.state_at(2.1) is None
    t.sticky = True
    assert t.state_at(9.0) is not None


def test_hidden_tracks_excluded():
    p = Project(tracks=[make_track(), make_track()])
    p.tracks[1].visible = False
    assert len(p.states_at(2.0)) == 1


def test_snap_ntsc():
    p = Project(source=SourceInfo(path="x", fps=Fraction(30000, 1001), duration_sec=10))
    t = p.snap(1.0)
    assert p.frame_of(t) == 30
    assert t == pytest.approx(30 * 1001 / 30000)
    assert p.snap(99) == pytest.approx(p.time_of(p.frame_of(10)))


def test_roundtrip_relative_path(tmp_path):
    video = tmp_path / "media" / "movie.vob"
    video.parent.mkdir()
    video.write_bytes(b"")
    src = SourceInfo(path=str(video), type="vob_single", fps=Fraction(30000, 1001),
                     width=720, height=480, duration_sec=12.5, sar=Fraction(8, 9), interlaced=True)
    p = Project(source=src, tracks=[make_track(sticky=True)])
    p.tracks[0].lock_aspect = False
    proj = tmp_path / "a.keyblur"
    p.save(str(proj))
    raw = json.loads(proj.read_text())
    assert raw["version"] == 1
    assert raw["source_video"]["path"] == "media/movie.vob"
    assert "id" not in raw["tracks"][0]["keyframes"][0]
    q = Project.load(str(proj))
    assert os.path.normpath(q.source.path) == os.path.normpath(str(video))
    assert q.source.fps == Fraction(30000, 1001)
    assert q.source.sar == Fraction(8, 9)
    assert q.tracks[0].sticky and not q.tracks[0].lock_aspect
    assert [k.to_dict() for k in q.tracks[0].keyframes] == [k.to_dict() for k in p.tracks[0].keyframes]


def test_load_spec_example_without_extra_fields(tmp_path):
    doc = {
        "version": 1,
        "source_video": {"path": "x.vob", "type": "vob_single", "fps": 29.97,
                         "width": 720, "height": 480, "duration_sec": 5400.0},
        "tracks": [{"id": "track-1", "name": "Face blur", "visible": True, "keyframes": [
            {"time": 3.0, "x": 0.6, "y": 0.5, "radius_x": 0.1, "radius_y": 0.1,
             "strength": 25, "interpolation": "linear"},
            {"time": 1.0, "x": 0.42, "y": 0.35, "radius_x": 0.08, "radius_y": 0.08,
             "strength": 25, "interpolation": "hold"}]}],
        "export_settings": {"output_path": "", "codec": "libx264", "crf": 18, "container": "mp4"},
    }
    f = tmp_path / "s.keyblur"
    f.write_text(json.dumps(doc))
    p = Project.load(str(f))
    assert p.source.fps == Fraction(30000, 1001)
    assert [k.time for k in p.tracks[0].keyframes] == [1.0, 3.0]  # sorted on load
    assert p.export_settings["preset"] == "medium"  # defaults filled in


def test_bad_files(tmp_path):
    f = tmp_path / "bad.keyblur"
    f.write_text("{not json")
    with pytest.raises(ProjectError):
        Project.load(str(f))
    f.write_text(json.dumps({"version": 99}))
    with pytest.raises(ProjectError):
        Project.load(str(f))
