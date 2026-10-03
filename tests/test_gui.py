"""Offscreen GUI smoke tests: canvas drag, auto-key, undo/redo, timeline retime, save/load."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from keyblur import video  # noqa: E402
from keyblur.commands import AddKeyframe  # noqa: E402
from keyblur.main_window import MainWindow  # noqa: E402
from keyblur.model import Project  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, single_vob, monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    monkeypatch.setattr(MainWindow, "autosave_path", staticmethod(lambda: str(tmp_path / "autosave.keyblur")))
    w = MainWindow()
    w.resize(1200, 800)
    w.show()
    QTest.qWaitForWindowExposed(w)
    src, playable = video.prepare_source(single_vob)
    w.doc.reset(Project(source=src), None, playable)
    app.processEvents()
    yield w
    w.doc.stack.setClean()
    w.close()


def scene_to_global(w, sx, sy):
    c = w.canvas
    return c.mapFromScene(sx, sy)


def drag(widget, a: QPoint, b: QPoint, steps=6, mods=Qt.NoModifier):
    QTest.mousePress(widget, Qt.LeftButton, mods, a)
    for i in range(1, steps + 1):
        p = a + (b - a) * (i / steps)
        QTest.mouseMove(widget, p)
    QTest.mouseRelease(widget, Qt.LeftButton, mods, b)


def test_loaded(win):
    assert win.doc.has_video
    assert win.slider.maximum() == win.doc.frame_count - 1
    assert win.raw_frame is not None and win.raw_frame.shape == (480, 640, 3)


def test_add_track_drag_autokey_undo(win, app):
    doc = win.doc
    win.add_track()
    tr = doc.selected_track
    assert tr is not None and len(tr.keyframes) == 1
    # move playhead and drag the (ghost) region: should auto-key a second keyframe
    doc.set_frame(60)
    app.processEvents()
    W, H = win.proxy_wh
    s = doc.display_state(tr)
    a = scene_to_global(win, s.x * W, s.y * H)
    b = scene_to_global(win, s.x * W + 100, s.y * H + 50)
    drag(win.canvas.viewport(), a, b)
    app.processEvents()
    assert len(tr.keyframes) == 2
    k2 = tr.keyframes[1]
    assert doc.project.frame_of(k2.time) == 60
    assert k2.x > 0.6 and k2.y > 0.55
    # a whole drag is a single undo step (macro), which also removes the auto-key
    doc.stack.undo()
    assert len(tr.keyframes) == 1
    doc.stack.redo()
    assert len(tr.keyframes) == 2 and tr.keyframes[1].x == pytest.approx(k2.x)
    # hold is the default: the first key stays put until the second
    mid = tr.state_at(doc.project.time_of(30))
    assert mid.x == pytest.approx(tr.keyframes[0].x)


def test_resize_handle_lock_aspect(win, app):
    doc = win.doc
    win.add_track()
    tr = doc.selected_track
    W, H = win.proxy_wh
    s = doc.display_state(tr)
    corner = scene_to_global(win, (s.x + s.radius_x) * W, (s.y + s.radius_y) * H)
    target = corner + QPoint(40, 5)
    drag(win.canvas.viewport(), corner, target)
    app.processEvents()
    k = tr.keyframes[0]
    assert k.radius_x > s.radius_x
    # locked: equal in display (proxy) pixels
    assert k.radius_x * W == pytest.approx(k.radius_y * H, rel=1e-6)


def test_inspector_and_interpolation(win, app):
    doc = win.doc
    win.add_track()
    tr = doc.selected_track
    win.inspector.f_strength.spin.setValue(60)
    app.processEvents()
    assert tr.keyframes[0].strength == pytest.approx(60)
    win.set_interpolation("linear")
    assert tr.keyframes[0].interpolation == "linear"
    doc.stack.undo()
    assert tr.keyframes[0].interpolation == "hold"


def test_timeline_retime(win, app):
    doc = win.doc
    doc.set_frame(10)
    win.add_track()
    tr = doc.selected_track
    tl = win.timeline.view
    tl.fit_all()
    app.processEvents()
    from keyblur.widgets.timeline import RULER_H, ROW_H
    y = RULER_H + ROW_H // 2
    a = QPoint(int(tl.t2x(tr.keyframes[0].time)), y)
    b = QPoint(int(tl.t2x(doc.project.time_of(50))), y)
    drag(tl, a, b)
    app.processEvents()
    assert abs(doc.project.frame_of(tr.keyframes[0].time) - 50) <= 1
    doc.stack.undo()
    assert doc.project.frame_of(tr.keyframes[0].time) == 10


def test_save_and_reload(win, app, tmp_path):
    doc = win.doc
    win.add_track()
    doc.set_frame(40)
    doc.edit_at_playhead(doc.selected_track, {"x": 0.8})
    doc.selected_track.sticky = True
    p = str(tmp_path / "proj.keyblur")
    doc.path = p
    assert win.save()
    assert doc.stack.isClean()
    loaded = Project.load(p)
    assert len(loaded.tracks) == 1
    assert loaded.tracks[0].sticky
    assert [round(k.x, 4) for k in loaded.tracks[0].keyframes] == [0.5, 0.8]


def test_playback_advances(win, app):
    # a real event loop (QTest.qWait holds the GIL and starves the decode thread)
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    win.toggle_play()
    QTimer.singleShot(1000, loop.quit)
    loop.exec()
    win.toggle_play()
    assert win.doc.frame >= 20  # ~30 fps source, allow start-up latency
    assert win.playback is None


def test_delete_keyframe_and_track(win, app):
    doc = win.doc
    win.add_track()
    win.delete_keyframe()
    assert doc.selected_track.keyframes == []
    win.delete_track()
    assert doc.project.tracks == []
    doc.stack.undo()
    doc.stack.undo()
    assert len(doc.project.tracks) == 1 and len(doc.project.tracks[0].keyframes) == 1


def test_shift_drag_range_then_blur_button(win, app):
    doc = win.doc
    tl = win.timeline.view
    tl.fit_all()
    app.processEvents()
    from keyblur.widgets.timeline import RULER_H
    y = RULER_H // 2
    a = QPoint(int(tl.t2x(doc.project.time_of(30))), y)
    b = QPoint(int(tl.t2x(doc.project.time_of(60))), y)
    QTest.mousePress(tl, Qt.LeftButton, Qt.ShiftModifier, a)
    for i in range(1, 7):
        QTest.mouseMove(tl, a + (b - a) * (i / 6))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.ShiftModifier, b)
    app.processEvents()
    assert doc.has_range
    assert abs(doc.in_frame - 30) <= 1 and abs(doc.out_frame - 60) <= 1
    assert win.btn_blur_range.isEnabled()
    win.btn_blur_range.click()
    win.btn_blur_range.click()
    app.processEvents()
    assert len(doc.project.tracks) == 2
    mid = doc.project.time_of(45)
    assert len(doc.project.states_at(mid)) == 2
    assert doc.project.states_at(doc.project.time_of(80)) == []


def test_goto_time_box(win, app):
    win.goto.setText("2.5")
    win._goto_time()
    assert win.doc.frame == win.doc.project.frame_of(2.5)
    win.goto.setText("f10")
    win._goto_time()
    assert win.doc.frame == 10


def test_set_all_interpolation(win, app):
    doc = win.doc
    win.add_track()
    tr = doc.selected_track
    for t, m in ((1.0, "linear"), (2.0, "off"), (3.0, "ease")):
        k = doc.new_keyframe_for(tr, t)
        k.interpolation = m
        doc.stack.push(AddKeyframe(doc, tr.id, k))
    before = [k.interpolation for k in tr.keyframes]
    assert doc.set_all_interpolation("hold", [tr]) == 2
    # "off" gaps survive
    assert [k.interpolation for k in tr.keyframes] == ["hold" if m != "off" else m for m in before]
    doc.stack.undo()  # one undo step reverts the lot
    assert [k.interpolation for k in tr.keyframes] == before


def test_box_select_keys_and_set_type(win, app):
    from keyblur.widgets.timeline import ROW_H, RULER_H
    doc = win.doc
    win.add_track()
    tr = doc.selected_track
    for t in (1.0, 2.0, 3.0, 4.0):
        if tr.keyframe_at(t) is None:
            doc.stack.push(AddKeyframe(doc, tr.id, doc.new_keyframe_for(tr, t)))
    view = win.timeline.view
    view.view_start, view.view_span = 0.0, 5.0
    view.update()
    app.processEvents()
    y = RULER_H + ROW_H // 2
    # Ctrl+drag a box around the keys at 2 s and 3 s only
    a = QPoint(int(view.t2x(1.5)), y - 10)
    b = QPoint(int(view.t2x(3.5)), y + 10)
    drag(view, a, b, mods=Qt.ControlModifier)
    app.processEvents()
    picked = [k for _t, k in doc.selected_keyframes()]
    assert [k.time for k in picked] == [2.0, 3.0]
    # Ctrl+click adds the key at 4 s
    QTest.mouseClick(view, Qt.LeftButton, Qt.ControlModifier, QPoint(int(view.t2x(4.0)), y))
    assert len(doc.selected_keys) == 3
    before = [k.interpolation for k in tr.keyframes]
    win.set_interpolation("ease")  # what Alt+3 does
    after = {k.time: k.interpolation for k in tr.keyframes}
    assert after[2.0] == after[3.0] == after[4.0] == "ease"
    assert after[1.0] == before[[k.time for k in tr.keyframes].index(1.0)]
    doc.stack.undo()  # one step
    assert [k.interpolation for k in tr.keyframes] == before
    n = len(tr.keyframes)
    win.delete_keyframe()  # Delete removes the whole selection
    assert len(tr.keyframes) == n - 3 and not doc.selected_keys
    # a plain click on empty timeline clears any selection
    win.select_all_keys()
    assert doc.selected_keys
    QTest.mouseClick(view, Qt.LeftButton, Qt.NoModifier, QPoint(int(view.t2x(2.5)), y))
    assert not doc.selected_keys
