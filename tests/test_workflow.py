"""The 'blur here, no blur there, several blurs over there' workflow."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fractions import Fraction  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from keyblur.commands import Document  # noqa: E402
from keyblur.model import Keyframe, Project, SourceInfo, Track, fmt_time, parse_time  # noqa: E402

FPS = Fraction(30)  # integer fps keeps frame/time math exact in assertions


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def doc(app):
    d = Document()
    src = SourceInfo(path="x.mp4", fps=FPS, width=640, height=480, duration_sec=10.0)
    d.reset(Project(source=src), None, "x.mp4")
    yield d
    d.stack.clear()  # delete commands while Qt still owns everything cleanly
    d.deleteLater()


def f(sec):  # seconds -> frame
    return int(sec * 30)


def blurring(doc, sec):
    """Tracks blurring at a time."""
    return [s.track_id for s in doc.project.states_at(sec)]


# ---- model -------------------------------------------------------------------
def test_off_keyframe_makes_gap():
    t = Track(keyframes=[Keyframe(1.0, interpolation="linear"), Keyframe(2.0, interpolation="off"),
                         Keyframe(3.0, x=0.7, interpolation="hold"), Keyframe(5.0, interpolation="off")])
    assert t.state_at(0.5) is None
    assert t.state_at(1.5) is not None
    assert t.state_at(2.0) is None and t.state_at(2.5) is None
    assert t.state_at(3.0).x == pytest.approx(0.7)
    assert t.state_at(4.99) is not None
    assert t.state_at(5.0) is None and t.state_at(9) is None
    assert t.active_segments(10) == [(1.0, 2.0), (3.0, 5.0)]


def test_linear_into_off_holds_values():
    t = Track(keyframes=[Keyframe(0.0, x=0.2), Keyframe(2.0, x=0.9, interpolation="off")])
    assert t.state_at(1.0).x == pytest.approx(0.2)


def test_sticky_ignored_after_off():
    t = Track(sticky=True, keyframes=[Keyframe(0.0), Keyframe(1.0, interpolation="off")])
    assert t.state_at(5.0) is None
    assert t.active_segments(10) == [(0.0, 1.0)]


def test_segments_merge_and_sticky_tail():
    t = Track(sticky=True, keyframes=[Keyframe(1.0), Keyframe(2.0, interpolation="hold")])
    assert t.active_segments(10) == [(1.0, 10.0)]


@pytest.mark.parametrize("text,expected", [
    ("3", 3.0), ("3.25", 3.25), ("1:02.5", 62.5), ("0:01:02", 62.0), ("f60", 2.0), ("60f", 2.0),
    ("2,5", 2.5), ("", None), ("abc", None),
])
def test_parse_time(text, expected):
    got = parse_time(text, 30.0)
    assert got == (pytest.approx(expected) if expected is not None else None)


def test_fmt_time():
    assert fmt_time(0) == "00:00.000"
    assert fmt_time(62.5) == "01:02.500"
    assert fmt_time(3723.004) == "1:02:03.004"


# ---- document operations ------------------------------------------------------
def test_user_workflow_blur_gap_multi(doc):
    """1-2 s: one blur · 2-3 s: nothing · 3-5 s: two blurs at once."""
    doc.set_range(f(1), f(2))
    a = doc.blur_range(new_track=True)
    doc.set_range(f(3), f(5))
    b = doc.blur_range(new_track=True)
    c = doc.blur_range(new_track=True)
    assert len(doc.project.tracks) == 3
    assert blurring(doc, 0.5) == []
    assert blurring(doc, 1.5) == [a.id]
    assert blurring(doc, 2.5) == []
    assert sorted(blurring(doc, 4.0)) == sorted([b.id, c.id])
    assert blurring(doc, 5.0) == [] and blurring(doc, 7) == []
    # simultaneous blurs start at different positions
    sb, sc = b.state_at(4.0), c.state_at(4.0)
    assert (sb.x, sb.y) != (sc.x, sc.y)
    # playhead moved to In, new track selected
    assert doc.frame == f(3) and doc.selected_track_id == c.id
    # the whole thing undoes step by step
    doc.stack.undo()
    doc.stack.undo()
    doc.stack.undo()
    assert doc.project.tracks == []


def test_same_track_two_ranges(doc):
    doc.set_range(f(1), f(2))
    tr = doc.blur_range(new_track=True)
    doc.set_range(f(4), f(6))
    assert doc.blur_range(new_track=False) is tr
    assert len(doc.project.tracks) == 1
    assert tr.active_segments(10) == [(1.0, 2.0), (4.0, 6.0)]
    doc.stack.undo()
    assert tr.active_segments(10) == [(1.0, 2.0)]


def test_blur_range_inside_existing_blur_keeps_rest(doc):
    doc.add_track()  # sticky track from 0 s to the end
    tr = doc.selected_track
    doc.set_range(f(3), f(4))
    doc.blur_range(new_track=False)
    assert tr.active_segments(10) == [(0.0, 10.0)]


def test_remove_blur_range_splits(doc):
    doc.add_track()  # 0..end
    tr = doc.selected_track
    doc.set_range(f(2), f(3))
    doc.remove_blur_range()
    assert tr.active_segments(10) == [(0.0, 2.0), (3.0, 10.0)]
    assert tr.state_at(2.5) is None
    doc.stack.undo()
    assert tr.active_segments(10) == [(0.0, 10.0)]


def test_remove_blur_range_keeps_motion_after(doc):
    from keyblur.commands import AddTrack
    tr = Track(name="m", keyframes=[Keyframe(0.0, x=0.0), Keyframe(4.0, x=0.8),
                                    Keyframe(5.0, x=0.8, interpolation="off")])
    doc.stack.push(AddTrack(doc, tr))
    doc.set_range(f(1), f(2))
    doc.remove_blur_range()
    # after the gap the blur resumes where the original motion was
    assert tr.state_at(2.0).x == pytest.approx(0.4)
    assert tr.state_at(3.0).x == pytest.approx(0.6)
    assert tr.state_at(1.5) is None


def test_toggle_at_playhead(doc):
    doc.add_track()
    tr = doc.selected_track
    doc.set_frame(f(2))
    doc.toggle_blur_at_playhead()
    assert tr.active_segments(10) == [(0.0, 2.0)]
    doc.set_frame(f(4))
    doc.toggle_blur_at_playhead()
    assert tr.active_segments(10) == [(0.0, 2.0), (4.0, 10.0)]
    doc.set_frame(f(2))
    doc.toggle_blur_at_playhead()  # turning on exactly on the off key re-enables it
    assert tr.active_segments(10) == [(0.0, 10.0)]


def test_drag_in_gap_turns_blur_on(doc):
    doc.set_range(f(1), f(2))
    tr = doc.blur_range(new_track=True)
    doc.set_frame(f(5))
    assert tr.state_at(5.0) is None
    doc.edit_at_playhead(tr, {"x": 0.3})
    assert tr.state_at(5.0).x == pytest.approx(0.3)
    assert tr.state_at(3.0) is None


def test_add_track_with_range_uses_range(doc):
    doc.set_range(f(2), f(3))
    tr = doc.add_track()
    assert tr.active_segments(10) == [(2.0, 3.0)]


def test_retime_clamps_between_neighbours(doc):
    doc.set_range(f(1), f(2))
    tr = doc.blur_range(new_track=True)
    k0 = tr.keyframes[0]
    doc.retime_keyframe(tr, k0, 5.0)
    assert k0.time == pytest.approx(2.0 - 1 / 30)


def test_duplicate_track(doc):
    doc.set_range(f(1), f(2))
    tr = doc.blur_range(new_track=True)
    dup = doc.duplicate_track()
    assert dup.id != tr.id and len(doc.project.tracks) == 2
    assert dup.active_segments(10) == tr.active_segments(10)
    assert dup.keyframes[0].id != tr.keyframes[0].id


def test_range_setters(doc):
    doc.set_frame(f(3))
    doc.set_in()
    doc.set_frame(f(1))
    doc.set_out()  # out before in -> in cleared
    assert doc.in_frame is None and doc.out_frame == f(1)
    doc.set_range(f(4), f(2))
    assert (doc.in_frame, doc.out_frame) == (f(2), f(4))
    assert doc.range_times() == (2.0, 4.0)
    doc.clear_range()
    assert not doc.has_range
