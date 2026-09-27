"""Main application window: menus, layout, playback, project I/O, export."""
from __future__ import annotations

import os
import queue
import time
from typing import Callable, Optional

import json
from datetime import datetime

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressDialog,
                               QPushButton, QSlider, QSplitter, QStyle, QVBoxLayout, QWidget)

from . import __version__, video
from .audio import AudioPlayer
from .blur import apply_blurs
from .commands import DeleteKeyframe, Document, EditKeyframe
from .export import ExportDialog, ExportWorker
from .model import INTERPOLATION_LABELS, INTERPOLATIONS, Project, ProjectError, parse_time
from .widgets.canvas import VideoCanvas
from .widgets.inspector import Inspector
from .widgets.timeline import Timeline, fmt_time
from .widgets.track_list import TrackList

PROJECT_FILTER = "KeyBlur project (*.keyblur)"
VIDEO_FILTER = ("Video files (*.vob *.mp4 *.mkv *.mov *.avi *.m4v *.mpg *.mpeg *.ts *.m2ts *.webm *.wmv *.flv);;"
                "All files (*)")
MAX_RECENT = 8


# --------------------------------------------------------------------------
# background helpers
# --------------------------------------------------------------------------
class TaskThread(QThread):
    """Runs fn(progress) in a thread; progress is a float 0..1 callback."""
    progress = Signal(float)
    finishedOk = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.finishedOk.emit(self.fn(self.progress.emit))
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.failed.emit(str(e))


class PlaybackThread(QThread):
    """Sequentially decodes proxy frames into a bounded queue."""

    def __init__(self, path, fps, size, start: int, parent=None):
        super().__init__(parent)
        self.path, self.fps, self.size, self.start_frame = path, fps, size, start
        self.q: queue.Queue = queue.Queue(maxsize=12)
        self._stop = False
        self.error: Optional[str] = None

    def stop(self):
        self._stop = True
        try:  # unblock a put()
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def run(self):
        try:
            reader = video.FrameReader(self.path, self.fps, out_size=self.size, cache_size=1)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.q.put(None)
            return
        try:
            for idx, arr in reader.iter_frames(self.start_frame):
                while not self._stop:
                    try:
                        self.q.put((idx, arr), timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if self._stop:
                    break
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
        finally:
            reader.close()
            if not self._stop:
                try:
                    self.q.put(None, timeout=1)
                except queue.Full:
                    pass


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.doc = Document(self)
        self.settings = QSettings("KeyBlur", "KeyBlur")
        self.reader: Optional[video.FrameReader] = None
        self.proxy_wh = (640, 360)
        self.raw_frame = None
        self.raw_frame_index = -1
        self.preview_blur = True
        self.playback: Optional[PlaybackThread] = None
        self.audio = AudioPlayer()
        self._play_t0 = 0.0
        self._play_f0 = 0
        self._pending = None
        self._export_worker: Optional[ExportWorker] = None
        self._task: Optional[TaskThread] = None

        self._build_ui()
        self._build_actions()

        self._seek_timer = QTimer(self, singleShot=True, interval=0)
        self._seek_timer.timeout.connect(self._load_frame)
        self._render_timer = QTimer(self, singleShot=True, interval=15)
        self._render_timer.timeout.connect(self._render)
        self._play_timer = QTimer(self, interval=5)
        self._play_timer.setTimerType(Qt.PreciseTimer)
        self._play_timer.timeout.connect(self._play_tick)

        self.doc.playheadChanged.connect(self._on_playhead)
        self.doc.projectChanged.connect(self._render_timer.start)
        self.doc.stack.cleanChanged.connect(self._update_title)
        self.doc.stack.indexChanged.connect(self._update_title)
        self.doc.sourceChanged.connect(self._on_source)
        self.doc.selectionChanged.connect(self._update_actions)
        self.doc.projectChanged.connect(self._update_actions)
        self.doc.playheadChanged.connect(self._update_actions)

        geo = self.settings.value("geometry")
        if geo is None or not self.restoreGeometry(geo):
            scr = QApplication.primaryScreen()
            avail = scr.availableGeometry() if scr else None
            if avail is not None and (avail.width() < 1600 or avail.height() < 950):
                self.resize(avail.width(), avail.height())
                self.setWindowState(Qt.WindowMaximized)
            else:
                self.resize(1500, 950)
        ff = self.settings.value("ffmpeg_path")
        if ff:
            video.set_tool_path("ffmpeg", ff)
        self.volume.setValue(int(self.settings.value("audio/volume", 100)))
        self.btn_mute.setChecked(str(self.settings.value("audio/muted", False)).lower() == "true")
        self._on_source()
        QTimer.singleShot(0, self._check_ffmpeg)
        self.setAcceptDrops(True)
        self._autosave_timer = QTimer(self, interval=60_000)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start()
        QTimer.singleShot(200, self._offer_recovery)

    # ---- UI construction ----------------------------------------------------
    def _build_ui(self):
        self.canvas = VideoCanvas(self.doc)
        self.track_list = TrackList(self.doc)
        self.inspector = Inspector(self.doc)
        self.timeline = Timeline(self.doc)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 0, 0, 0)
        rl.addWidget(QLabel("<b>Tracks</b>"))
        rl.addWidget(self.track_list, 1)
        rl.addWidget(self.inspector, 2)
        right.setMinimumWidth(300)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.canvas)
        top.addWidget(right)
        top.setStretchFactor(0, 1)
        top.setSizes([1050, 350])

        # transport
        st = self.style()

        def tbtn(icon=None, text="", tip="", slot=None):
            b = QPushButton(text)
            if icon is not None:
                b.setIcon(st.standardIcon(icon))
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            if slot:
                b.clicked.connect(slot)
            return b

        self.btn_prev = tbtn(QStyle.SP_MediaSkipBackward, tip="Previous keyframe ([)",
                             slot=lambda: self.jump_keyframe(-1))
        self.btn_frame_back = tbtn(QStyle.SP_MediaSeekBackward, tip="Previous frame (Left)",
                                   slot=lambda: self.step(-1))
        self.btn_play = tbtn(QStyle.SP_MediaPlay, tip="Play / Pause (Space)", slot=self.toggle_play)
        self.btn_frame_fwd = tbtn(QStyle.SP_MediaSeekForward, tip="Next frame (Right)",
                                  slot=lambda: self.step(1))
        self.btn_next = tbtn(QStyle.SP_MediaSkipForward, tip="Next keyframe (])",
                             slot=lambda: self.jump_keyframe(1))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setFocusPolicy(Qt.NoFocus)
        self.time_label = QLabel()
        self.time_label.setMinimumWidth(250)
        f = self.time_label.font()
        f.setFamily("Consolas")
        f.setPointSize(10)
        self.time_label.setFont(f)
        self.goto = QLineEdit()
        self.goto.setPlaceholderText("Go to: 1:02.5 / 62.5 / f120")
        self.goto.setToolTip("Type a time and press Enter (Ctrl+G).\n"
                             "Examples: 3  ·  3.25  ·  1:02.5  ·  0:01:02  ·  f120 (frame 120)")
        self.goto.setFixedWidth(190)
        self.goto.returnPressed.connect(self._goto_time)
        self.btn_mute = tbtn(QStyle.SP_MediaVolume, tip="Mute / unmute preview audio (M)")
        self.btn_mute.setCheckable(True)
        self.btn_mute.toggled.connect(self._set_muted)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(80)
        self.volume.setFocusPolicy(Qt.NoFocus)
        self.volume.setToolTip("Preview volume (the export always keeps the original audio)")
        self.volume.valueChanged.connect(self._set_volume)

        tr = QHBoxLayout()
        for w in (self.btn_prev, self.btn_frame_back, self.btn_play, self.btn_frame_fwd, self.btn_next):
            tr.addWidget(w)
        tr.addWidget(self.slider, 1)
        tr.addWidget(self.time_label)
        tr.addWidget(self.goto)
        tr.addWidget(self.btn_mute)
        tr.addWidget(self.volume)

        # blur workflow buttons
        self.btn_blur_range = tbtn(text="Blur In→Out (new track)",
                                   tip="Blur the In/Out range with a new blur circle (Ctrl+B).\n"
                                       "Press again for more simultaneous blurs.",
                                   slot=lambda: self.doc.blur_range(new_track=True))
        self.btn_blur_range_sel = tbtn(text="Add In→Out to track",
                                       tip="Blur the In/Out range on the selected track (Ctrl+Shift+B)",
                                       slot=lambda: self.doc.blur_range(new_track=False))
        self.btn_unblur_range = tbtn(text="Remove blur In→Out",
                                     tip="Turn the selected track's blur off inside In/Out (Shift+Del)",
                                     slot=self.doc.remove_blur_range)
        self.btn_toggle = tbtn(text="Blur On/Off here",
                               tip="Turn the selected track's blur off (or back on) from the playhead (X)",
                               slot=self.toggle_blur)
        self.btn_addkf = tbtn(text="+ Keyframe", tip="Add keyframe to the selected track (K)",
                              slot=self.add_keyframe)
        self.btn_delkf = tbtn(text="− Keyframe", tip="Delete the selected track's keyframe at the playhead (Del)",
                              slot=self.delete_keyframe)
        self.new_interp = QComboBox()
        for m in INTERPOLATIONS:
            if m != "off":
                self.new_interp.addItem(m.capitalize(), m)
        self.new_interp.setCurrentIndex(INTERPOLATIONS.index("linear"))
        self.new_interp.setToolTip("Movement type for newly created keyframes")
        self.new_interp.setFocusPolicy(Qt.NoFocus)
        self.chk_blur = QCheckBox("Preview blur")
        self.chk_blur.setChecked(True)
        self.chk_blur.setToolTip("Toggle the blur in the preview (B)")
        self.chk_outline = QCheckBox("Outlines")
        self.chk_outline.setChecked(True)
        self.chk_outline.setToolTip("Show circles and handles (H)")
        for w in (self.chk_blur, self.chk_outline):
            w.setFocusPolicy(Qt.NoFocus)
        self.btn_blur_range.setStyleSheet("QPushButton{background:#2d5a8c;color:white;padding:3px 10px;}"
                                          "QPushButton:disabled{background:#333;color:#777;}")

        tr2 = QHBoxLayout()
        for w in (self.btn_blur_range, self.btn_blur_range_sel, self.btn_unblur_range):
            tr2.addWidget(w)
        tr2.addSpacing(12)
        for w in (self.btn_toggle, self.btn_addkf, self.btn_delkf):
            tr2.addWidget(w)
        tr2.addSpacing(12)
        tr2.addWidget(QLabel("New keys:"))
        tr2.addWidget(self.new_interp)
        tr2.addStretch()
        tr2.addWidget(self.chk_blur)
        tr2.addWidget(self.chk_outline)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(4, 0, 4, 2)
        bl.addWidget(self.timeline, 1)
        bl.addLayout(tr)
        bl.addLayout(tr2)

        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(bottom)
        main.setStretchFactor(0, 1)
        main.setSizes([600, 340])
        main.setCollapsible(0, False)
        main.setCollapsible(1, False)
        self.setCentralWidget(main)
        self.statusBar()

        self.slider.valueChanged.connect(self._slider_moved)
        self.new_interp.currentIndexChanged.connect(
            lambda _i: setattr(self.doc, "default_interpolation", self.new_interp.currentData()))
        self.chk_blur.toggled.connect(self._set_preview_blur)
        self.chk_outline.toggled.connect(self._set_outlines)
        self.doc.message.connect(lambda m: self.statusBar().showMessage(m, 4000))
        self.doc.rangeChanged.connect(self._update_actions)

    def _act(self, menu, text, slot, shortcut=None, tip=None) -> QAction:
        a = QAction(text, self)
        if shortcut:
            if isinstance(shortcut, (list, tuple)):
                a.setShortcuts([QKeySequence(s) for s in shortcut])
            else:
                a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setStatusTip(tip)
        a.triggered.connect(slot)
        if menu is not None:
            menu.addAction(a)
        else:
            self.addAction(a)
        return a

    def _build_actions(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        self._act(m, "&New Project", self.new_project, QKeySequence.New)
        self._act(m, "&Open Project…", self.open_project_dialog, QKeySequence.Open)
        self.recent_menu = m.addMenu("Open &Recent")
        m.addSeparator()
        self._act(m, "Open &Video…", self.open_video_dialog, "Ctrl+Shift+O")
        self._act(m, "Import VIDEO_&TS Folder…", self.open_video_ts_dialog)
        self.act_relink = self._act(m, "Re&link Source Video…", self.relink_dialog)
        m.addSeparator()
        self.act_save = self._act(m, "&Save", self.save, QKeySequence.Save)
        self._act(m, "Save &As…", self.save_as, QKeySequence.SaveAs)
        m.addSeparator()
        self.act_export = self._act(m, "&Export Video…", self.export_dialog, "Ctrl+E")
        m.addSeparator()
        self._act(m, "Set ffmpeg Location…", self.set_ffmpeg_dialog)
        m.addSeparator()
        self._act(m, "E&xit", self.close, QKeySequence.Quit)

        e = mb.addMenu("&Edit")
        undo = self.doc.stack.createUndoAction(self, "&Undo")
        undo.setShortcut(QKeySequence.Undo)
        redo = self.doc.stack.createRedoAction(self, "&Redo")
        redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        e.addAction(undo)
        e.addAction(redo)
        e.addSeparator()
        self.act_add_track = self._act(e, "Add &Track (range or from playhead)", self.add_track, "Ctrl+T")
        self.act_dup_track = self._act(e, "D&uplicate Track", self.doc.duplicate_track, "Ctrl+D")
        self.act_del_track = self._act(e, "Delete Track", self.delete_track, "Ctrl+Shift+Delete")
        e.addSeparator()
        self.act_add_kf = self._act(e, "Add &Keyframe at Playhead", self.add_keyframe, "K")
        self.act_del_kf = self._act(e, "&Delete Keyframe at Playhead", self.delete_keyframe, QKeySequence.Delete)
        self.act_toggle = self._act(e, "Blur &On/Off from Playhead", self.toggle_blur, "X")
        interp = e.addMenu("After Keyframe (&Interpolation)")
        for i, mode in enumerate(INTERPOLATIONS):
            self._act(interp, INTERPOLATION_LABELS[mode],
                      lambda _c=False, m_=mode: self.set_interpolation(m_), f"Alt+{i + 1}")

        r = mb.addMenu("&Range")
        self._act(r, "Set &In at Playhead", lambda: self.doc.set_in(), "I")
        self._act(r, "Set &Out at Playhead", lambda: self.doc.set_out(), "O")
        self._act(r, "Go to In", lambda: self.doc.in_frame is not None and self.doc.set_frame(self.doc.in_frame),
                  "Shift+I")
        self._act(r, "Go to Out", lambda: self.doc.out_frame is not None and self.doc.set_frame(self.doc.out_frame),
                  "Shift+O")
        self._act(r, "Clear In/Out", self.doc.clear_range, "Alt+X")
        r.addSeparator()
        self.act_blur_range = self._act(r, "&Blur In→Out as New Track", lambda: self.doc.blur_range(True), "Ctrl+B")
        self.act_blur_range_sel = self._act(r, "Blur In→Out on &Selected Track",
                                            lambda: self.doc.blur_range(False), "Ctrl+Shift+B")
        self.act_unblur = self._act(r, "&Remove Blur In→Out from Selected Track", self.doc.remove_blur_range,
                                    "Shift+Delete")
        r.addSeparator()
        self._act(r, "Zoom Timeline to In→Out", self.timeline.view.zoom_to_range, "Ctrl+Shift+0")

        v = mb.addMenu("&View")
        self._act(v, "Play / Pause", self.toggle_play, "Space")
        self._act(v, "Next Frame", lambda: self.step(1), "Right")
        self._act(v, "Previous Frame", lambda: self.step(-1), "Left")
        self._act(v, "Forward 1 s", lambda: self.step_seconds(1), "Shift+Right")
        self._act(v, "Back 1 s", lambda: self.step_seconds(-1), "Shift+Left")
        self._act(v, "Next Keyframe", lambda: self.jump_keyframe(1), "]")
        self._act(v, "Previous Keyframe", lambda: self.jump_keyframe(-1), "[")
        self._act(v, "Go to Start", lambda: self.doc.set_frame(0), "Home")
        self._act(v, "Go to End", lambda: self.doc.set_frame(self.doc.frame_count - 1), "End")
        self._act(v, "Go to Time…", self._focus_goto, "Ctrl+G")
        self._act(v, "Mute Audio", lambda: self.btn_mute.toggle(), "M")
        v.addSeparator()
        self._act(v, "Toggle Preview Blur", lambda: self.chk_blur.toggle(), "B")
        self._act(v, "Toggle Outlines", lambda: self.chk_outline.toggle(), "H")
        v.addSeparator()
        self._act(v, "Timeline: Zoom In", lambda: self.timeline.view.zoom(1 / 1.5), ["=", "Ctrl+="])
        self._act(v, "Timeline: Zoom Out", lambda: self.timeline.view.zoom(1.5), ["-", "Ctrl+-"])
        self._act(v, "Timeline: Zoom to Fit", self.timeline.fit_all, "Ctrl+0")

        h = mb.addMenu("&Help")
        self._act(h, "User &Guide", self.show_guide, "F1")
        self._act(h, "&Shortcuts", self.show_shortcuts, "Shift+F1")
        self._act(h, "&About KeyBlur", self.show_about)
        self._refresh_recent()

    # ---- state / titles ---------------------------------------------------------
    def _update_title(self, *_):
        name = os.path.basename(self.doc.path) if self.doc.path else "Untitled"
        dirty = "" if self.doc.stack.isClean() else "*"
        self.setWindowTitle(f"{name}{dirty} - KeyBlur")

    def _update_actions(self, *_):
        has = self.doc.has_video
        tr = self.doc.selected_track
        for a in (self.act_save, self.act_export, self.act_add_track):
            a.setEnabled(has)
        self.act_del_track.setEnabled(tr is not None)
        self.act_add_kf.setEnabled(has and tr is not None)
        kf = self.doc.keyframe_at_playhead(tr) if tr else None
        self.act_del_kf.setEnabled(kf is not None)
        self.btn_addkf.setEnabled(has and tr is not None and kf is None)
        self.btn_delkf.setEnabled(kf is not None)
        for w in (self.btn_play, self.btn_prev, self.btn_next, self.btn_frame_back, self.btn_frame_fwd,
                  self.slider, self.goto):
            w.setEnabled(has)
        rng = has and self.doc.has_range
        self.btn_blur_range.setEnabled(rng)
        self.act_blur_range.setEnabled(rng)
        for w in (self.btn_blur_range_sel, self.btn_unblur_range, self.act_blur_range_sel, self.act_unblur):
            w.setEnabled(rng and tr is not None)
        self.btn_toggle.setEnabled(has and tr is not None)
        self.act_toggle.setEnabled(has and tr is not None)
        self.act_dup_track.setEnabled(tr is not None)
        if tr is not None and has:
            on = tr.state_at(self.doc.time) is not None
            self.btn_toggle.setText("Blur OFF from here" if on else "Blur ON from here")
        else:
            self.btn_toggle.setText("Blur On/Off here")
        self.btn_blur_range.setToolTip(
            ("Blur the In/Out range with a new blur circle (Ctrl+B).\n"
             "Press again for more simultaneous blurs.") if rng else
            "First set a range: press I at the start and O at the end\n(or Shift+drag on the timeline).")
        self.act_relink.setEnabled(self.doc.project.source is not None)

    def _on_source(self):
        self._stop_playback()
        if self.reader:
            self.reader.close()
            self.reader = None
        self.raw_frame = None
        self.raw_frame_index = -1
        src = self.doc.project.source
        if self.doc.has_video:
            self.proxy_wh = video.proxy_size(src)
            try:
                self.reader = video.FrameReader(self.doc.playable_path, src.fps, out_size=self.proxy_wh)
            except video.VideoError as e:
                self._error("Could not open video", str(e))
            self.slider.blockSignals(True)
            self.slider.setRange(0, self.doc.frame_count - 1)
            self.slider.setValue(self.doc.frame)
            self.slider.blockSignals(False)
            ar = f"{src.width}x{src.height}"
            if src.sar != 1:
                ar += f" (display {src.display_aspect:.2f}:1)"
            self.statusBar().showMessage(
                f"{os.path.basename(src.path)} - {ar}, {float(src.fps):.3f} fps, "
                f"{fmt_time(src.duration_sec, float(src.fps))}"
                + (", interlaced" if src.interlaced else ""))
            self._load_frame()
        else:
            self.canvas.clear_frame()
            self.slider.setRange(0, 0)
        self._update_time_label()
        self._update_title()
        self._update_actions()

    # ---- frames & rendering -----------------------------------------------------
    def _on_playhead(self, frame: int):
        self.slider.blockSignals(True)
        self.slider.setValue(frame)
        self.slider.blockSignals(False)
        self._update_time_label()
        if self.playback is None:
            self._seek_timer.start()  # coalesce rapid scrubbing

    def _slider_moved(self, v: int):
        if self.playback is not None:
            self._stop_playback()
        self.doc.set_frame(v)

    def _update_time_label(self):
        self.time_label.setText(f"{fmt_time(self.doc.time)} / {fmt_time(self.doc.project.duration)}"
                                f"   frame {self.doc.frame}")

    def _focus_goto(self):
        self.goto.setFocus()
        self.goto.selectAll()

    def _goto_time(self):
        t = parse_time(self.goto.text(), float(self.doc.project.fps))
        if t is None:
            self.statusBar().showMessage("Could not read that time. Try 3.5, 1:02.25 or f120.", 4000)
            return
        self._stop_playback()
        self.doc.set_frame(self.doc.project.frame_of(t))
        self.goto.clear()
        self.canvas.setFocus()

    def _load_frame(self):
        if self.reader is None:
            return
        idx = self.doc.frame
        if idx == self.raw_frame_index and self.raw_frame is not None:
            return
        try:
            arr = self.reader.frame_at(idx)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Decode error at frame {idx}: {e}", 5000)
            return
        if arr is None:
            return
        self.raw_frame, self.raw_frame_index = arr, idx
        self._render()

    def _render(self):
        if self.raw_frame is None or not self.doc.has_video:
            return
        src = self.doc.project.source
        if self.preview_blur:
            t = self.doc.project.time_of(self.raw_frame_index)
            scale = self.proxy_wh[1] / src.height
            img = apply_blurs(self.raw_frame, self.doc.project.states_at(t), scale)
        else:
            img = self.raw_frame
        self.canvas.set_frame(img)

    def _set_preview_blur(self, on: bool):
        self.preview_blur = on
        self._render()

    def _set_outlines(self, on: bool):
        self.canvas.show_outlines = on
        self.canvas.viewport().update()

    # ---- playback ----------------------------------------------------------------
    def toggle_play(self):
        if self.playback is not None:
            self._stop_playback()
        else:
            self._start_playback()

    def _start_playback(self):
        if not self.doc.has_video:
            return
        start = self.doc.frame
        if start >= self.doc.frame_count - 1:
            start = 0
            self.doc.set_frame(0)
        src = self.doc.project.source
        self.playback = PlaybackThread(self.doc.playable_path, src.fps, self.proxy_wh, start, self)
        self.playback.start()
        self._play_f0 = start
        self._pending = None
        self._play_t0 = time.perf_counter() + 0.05
        self.audio.start(self.doc.playable_path, start / float(src.fps), self._play_t0)
        self._play_timer.start()
        self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def _stop_playback(self):
        if self.playback is None:
            return
        self._play_timer.stop()
        self.audio.stop()
        pb = self.playback
        self.playback = None
        pb.stop()
        pb.wait(2000)
        self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self._seek_timer.start()

    def _play_tick(self):
        pb = self.playback
        if pb is None:
            return
        self.audio.pump()
        fps = float(self.doc.project.fps)
        due = self._play_f0 + int((time.perf_counter() - self._play_t0) * fps)
        latest = None
        while True:
            if self._pending is not None:
                item, self._pending = self._pending, None
            else:
                try:
                    item = pb.q.get_nowait()
                except queue.Empty:
                    break
            if item is None:  # end of stream
                err = pb.error
                self._stop_playback()
                if err:
                    self.statusBar().showMessage(f"Playback error: {err}", 5000)
                break
            if item[0] > due:  # not yet time for this frame
                self._pending = item
                break
            latest = item  # older due frames are dropped
        if latest is not None:
            idx, arr = latest
            self.raw_frame, self.raw_frame_index = arr, idx
            self.doc.set_frame(idx)
            self._render()

    def _set_muted(self, muted: bool):
        self.audio.set_muted(muted)
        self.btn_mute.setIcon(self.style().standardIcon(QStyle.SP_MediaVolumeMuted if muted
                                                        else QStyle.SP_MediaVolume))
        self.settings.setValue("audio/muted", muted)

    def _set_volume(self, v: int):
        self.audio.set_volume(v / 100)
        self.settings.setValue("audio/volume", v)

    def step(self, n: int):
        self._stop_playback()
        self.doc.set_frame(self.doc.frame + n)

    def step_seconds(self, s: float):
        self.step(int(round(s * float(self.doc.project.fps))))

    def jump_keyframe(self, direction: int):
        if not self.doc.has_video:
            return
        self._stop_playback()
        tr = self.doc.selected_track
        tracks = [tr] if tr else self.doc.project.tracks
        frames = sorted({self.doc.project.frame_of(k.time) for t in tracks for k in t.keyframes})
        cur = self.doc.frame
        if direction > 0:
            nxt = next((f for f in frames if f > cur), None)
        else:
            nxt = next((f for f in reversed(frames) if f < cur), None)
        if nxt is not None:
            self.doc.set_frame(nxt)

    # ---- editing -----------------------------------------------------------------
    def add_track(self):
        if self.doc.has_video:
            self.doc.add_track()

    def delete_track(self):
        self.track_list._delete()

    def add_keyframe(self):
        tr = self.doc.selected_track
        if not self.doc.has_video:
            return
        if tr is None:
            self.doc.add_track()
            return
        self.doc.ensure_keyframe_at_playhead(tr)

    def delete_keyframe(self):
        tr = self.doc.selected_track
        k = self.doc.keyframe_at_playhead(tr) if tr else None
        if k is not None:
            self.doc.stack.push(DeleteKeyframe(self.doc, tr.id, k.id))

    def set_interpolation(self, mode: str):
        tr = self.doc.selected_track
        seg = self.doc.segment_keyframe(tr) if tr else None
        if seg is not None and seg.interpolation != mode:
            self.doc.stack.push(EditKeyframe(self.doc, tr.id, seg.id, {"interpolation": mode}))

    def toggle_blur(self):
        if self.doc.has_video and self.doc.selected_track is not None:
            self.doc.toggle_blur_at_playhead()

    # ---- project I/O -------------------------------------------------------------
    def _confirm_discard(self) -> bool:
        if self.doc.stack.isClean() or not self.doc.has_video:
            return True
        r = QMessageBox.question(self, "Unsaved changes", "Save changes to the current project?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save()
        if r == QMessageBox.Discard:
            self._remove_autosave()
            return True
        return False

    def new_project(self):
        if self._confirm_discard():
            self.doc.reset(Project(), None, None)

    def open_project_dialog(self):
        if not self._confirm_discard():
            return
        p, _ = QFileDialog.getOpenFileName(self, "Open Project", self._last_dir(), PROJECT_FILTER)
        if p:
            self.open_project(p)

    def open_project(self, path: str):
        try:
            proj = Project.load(path)
        except (OSError, ProjectError) as e:
            self._error("Could not open project", str(e))
            return
        self._remember_dir(path)
        if proj.source is None:
            self.doc.reset(proj, path, None)
            self._add_recent(path)
            return
        src = proj.source
        exists = os.path.isdir(src.path) if src.type == "vob_video_ts" else os.path.isfile(src.path)
        if not exists:
            r = QMessageBox.warning(
                self, "Source video missing",
                f"The source video for this project was not found:\n\n{src.path}\n\nLocate it now?",
                QMessageBox.Yes | QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                return
            newp = self._ask_source_path(src.type)
            if not newp:
                return
            src.path = newp
        self._prepare_and_load(src.path, src.title_set, project=proj, project_path=path)
        self._add_recent(path)

    def save(self) -> bool:
        if not self.doc.path:
            return self.save_as()
        try:
            self.doc.project.save(self.doc.path)
        except OSError as e:
            self._error("Could not save", str(e))
            return False
        self.doc.stack.setClean()
        self._remove_autosave()
        self._add_recent(self.doc.path)
        self.statusBar().showMessage(f"Saved {self.doc.path}", 3000)
        return True

    def save_as(self) -> bool:
        base = self.doc.path
        if not base and self.doc.project.source:
            base = os.path.splitext(self.doc.project.source.path.rstrip("\\/"))[0] + ".keyblur"
        p, _ = QFileDialog.getSaveFileName(self, "Save Project", base or self._last_dir(), PROJECT_FILTER)
        if not p:
            return False
        if not p.lower().endswith(".keyblur"):
            p += ".keyblur"
        self.doc.path = p
        self._remember_dir(p)
        return self.save()

    # ---- video loading -------------------------------------------------------------
    def open_video_dialog(self):
        if not self._confirm_discard():
            return
        p, _ = QFileDialog.getOpenFileName(self, "Open Video", self._last_dir(), VIDEO_FILTER)
        if p:
            self._remember_dir(p)
            self.open_video(p)

    def open_video_ts_dialog(self):
        if not self._confirm_discard():
            return
        p = QFileDialog.getExistingDirectory(self, "Select VIDEO_TS folder (or its parent)", self._last_dir())
        if p:
            self._remember_dir(p)
            self.open_video(p)

    def open_video(self, path: str):
        """Load a new video. Keeps tracks if a project is open and the user agrees."""
        title_set = None
        if os.path.isdir(path):
            title_set = self._choose_title_set(path)
            if title_set is False:
                return
        project = None
        if self.doc.project.tracks:
            r = QMessageBox.question(self, "Keep tracks?",
                                     "Keep the existing blur tracks with the new video?",
                                     QMessageBox.Yes | QMessageBox.No)
            if r == QMessageBox.Yes:
                project = self.doc.project
        self._prepare_and_load(path, title_set, project=project, project_path=self.doc.path if project else None)

    def _choose_title_set(self, folder: str):
        try:
            sets = video.find_title_sets(folder)
        except OSError as e:
            self._error("Cannot read folder", str(e))
            return False
        if not sets:
            self._error("Not a DVD folder", "No VTS_xx_N.VOB files found in this folder.")
            return False
        if len(sets) == 1:
            return next(iter(sets))
        main = max(sets, key=lambda k: video.title_set_size(sets[k]))
        items, keys = [], []
        for k, files in sets.items():
            mb = video.title_set_size(files) / 1e6
            items.append(f"Title set {k}: {len(files)} VOB file(s), {mb:,.0f} MB" + ("  (main)" if k == main else ""))
            keys.append(k)
        choice, ok = QInputDialog.getItem(self, "Choose title", "This disc has several title sets:",
                                          items, keys.index(main), False)
        return keys[items.index(choice)] if ok else False

    def _prepare_and_load(self, path: str, title_set, project: Optional[Project], project_path: Optional[str],
                          dirty: bool = False):
        needs_remux = video.source_type_for(path) != "generic"
        dlg = QProgressDialog("Preparing video…" if not needs_remux else
                              "Remuxing DVD video (lossless, one-time)…", None, 0, 1000, self)
        dlg.setWindowTitle("KeyBlur")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        dlg.setValue(0)

        task = TaskThread(lambda prog: video.prepare_source(path, title_set, progress=prog), self)
        task.progress.connect(lambda f: dlg.setValue(int(f * 1000)))

        def ok(result):
            dlg.close()
            src, playable = result
            proj = project or Project()
            old = proj.source
            if old is not None and project is not None and (old.width, old.height) != (src.width, src.height):
                QMessageBox.information(self, "Resolution changed",
                                        "The video resolution differs from the project. Blur positions are "
                                        "stored relative to the frame, so they are kept proportionally.")
            proj.source = src
            self.doc.reset(proj, project_path, playable)
            if project is not None and project_path and old is not None and old.path != src.path:
                self.doc.stack.resetClean()  # relinked: needs saving
            if dirty:
                self.doc.stack.resetClean()

        def fail(msg):
            dlg.close()
            self._error("Could not load video", msg)

        task.finishedOk.connect(ok)
        task.failed.connect(fail)
        self._task = task
        task.start()

    def _ask_source_path(self, stype: str) -> Optional[str]:
        if stype == "vob_video_ts":
            p = QFileDialog.getExistingDirectory(self, "Locate VIDEO_TS folder", self._last_dir())
        else:
            p, _ = QFileDialog.getOpenFileName(self, "Locate source video", self._last_dir(), VIDEO_FILTER)
        return p or None

    def relink_dialog(self):
        src = self.doc.project.source
        if not src:
            return
        p = self._ask_source_path(src.type)
        if p:
            ts = self._choose_title_set(p) if os.path.isdir(p) else None
            if ts is False:
                return
            self._prepare_and_load(p, ts, project=self.doc.project, project_path=self.doc.path)

    # ---- export --------------------------------------------------------------------
    def export_dialog(self):
        if not self.doc.has_video or self._export_worker is not None:
            return
        self._stop_playback()
        src = self.doc.project.source
        ext = self.doc.project.export_settings.get("container", "mp4")
        default = os.path.splitext(src.path.rstrip("\\/"))[0] + f"_blurred.{ext}"
        dlg = ExportDialog(self.doc.project, default, self)
        if dlg.exec() != ExportDialog.Accepted:
            return
        settings = dlg.settings()
        out = settings["output_path"]
        if os.path.normcase(os.path.abspath(out)) in (os.path.normcase(os.path.abspath(src.path)),
                                                       os.path.normcase(os.path.abspath(self.doc.playable_path))):
            self._error("Invalid output", "The output file cannot overwrite the source video.")
            return
        if os.path.exists(out):
            if QMessageBox.question(self, "Overwrite?", f"{out} exists. Overwrite?") != QMessageBox.Yes:
                return
        if self.doc.project.export_settings != settings:
            self.doc.project.export_settings = settings
            self.doc.stack.resetClean()

        total = src.frame_count
        prog = QProgressDialog("Exporting…", "Cancel", 0, total, self)
        prog.setWindowTitle("Export")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        worker = ExportWorker(self.doc.project, self.doc.playable_path, out, settings, self)
        t0 = time.time()

        def on_progress(n, tot):
            prog.setValue(n)
            el = time.time() - t0
            eta = el / n * (tot - n) if n else 0
            prog.setLabelText(f"Frame {n:,} / {tot:,}   ({n / max(el, 1e-6):.1f} fps, ETA {int(eta // 60)}:{int(eta % 60):02d})")

        def on_done(ok, msg):
            prog.close()
            self._export_worker = None
            if ok:
                QMessageBox.information(self, "Export complete", msg)
            else:
                (QMessageBox.information if "cancel" in msg.lower() else QMessageBox.critical)(
                    self, "Export", msg)

        worker.progress.connect(on_progress)
        worker.done.connect(on_done)
        prog.canceled.connect(worker.cancel)
        self._export_worker = worker
        worker.start()

    # ---- misc ------------------------------------------------------------------------
    def _check_ffmpeg(self):
        try:
            video.tool("ffmpeg")
            video.tool("ffprobe")
        except video.VideoError as e:
            QMessageBox.warning(self, "ffmpeg not found", f"{e}\n\nUse File > Set ffmpeg Location…")

    def set_ffmpeg_dialog(self):
        p, _ = QFileDialog.getOpenFileName(self, "Locate ffmpeg.exe", "", "ffmpeg (ffmpeg*);;All files (*)")
        if p:
            video.set_tool_path("ffmpeg", p)
            probe = os.path.join(os.path.dirname(p), "ffprobe" + os.path.splitext(p)[1])
            if os.path.isfile(probe):
                video.set_tool_path("ffprobe", probe)
            self.settings.setValue("ffmpeg_path", p)

    def _last_dir(self) -> str:
        return self.settings.value("last_dir", os.path.expanduser("~"))

    def _remember_dir(self, path: str):
        self.settings.setValue("last_dir", os.path.dirname(os.path.abspath(path.rstrip("\\/"))))

    def _recent(self) -> list[str]:
        v = self.settings.value("recent", [])
        return [v] if isinstance(v, str) else list(v or [])

    def _add_recent(self, path: str):
        path = os.path.abspath(path)
        rec = [p for p in self._recent() if os.path.normcase(p) != os.path.normcase(path)]
        self.settings.setValue("recent", [path] + rec[:MAX_RECENT - 1])
        self._refresh_recent()

    def _refresh_recent(self):
        self.recent_menu.clear()
        rec = self._recent()
        for p in rec:
            a = self.recent_menu.addAction(p)
            a.triggered.connect(lambda _c=False, p_=p: self._confirm_discard() and self.open_project(p_))
        self.recent_menu.setEnabled(bool(rec))

    def _error(self, title: str, msg: str):
        QMessageBox.critical(self, title, msg)

    def show_guide(self):
        guide = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "GUIDE.md")
        if os.path.isfile(guide):
            QDesktopServices.openUrl(QUrl.fromLocalFile(guide))
        else:
            self.show_shortcuts()

    def show_shortcuts(self):
        QMessageBox.information(self, "Shortcuts", (
            "PLAYBACK\n"
            "Space  Play / pause\n"
            "Left / Right  Previous / next frame\n"
            "Shift+Left / Right  Back / forward 1 second\n"
            "[ / ]  Previous / next keyframe\n"
            "Home / End  Start / end,   Ctrl+G  Go to time\n"
            "M  Mute / unmute audio\n\n"
            "RANGE (the easy way)\n"
            "I / O  Set In / Out at playhead   (or Shift+drag on the timeline)\n"
            "Ctrl+B  Blur In→Out with a NEW blur (press again for more)\n"
            "Ctrl+Shift+B  Blur In→Out on the selected track\n"
            "Shift+Del  Remove blur In→Out from the selected track\n"
            "Alt+X  Clear In/Out\n\n"
            "EDITING\n"
            "X  Turn the selected blur off / on from the playhead\n"
            "K / Del  Add / delete keyframe at playhead\n"
            "Alt+1..4  After key: Hold / Linear / Ease / Off\n"
            "Ctrl+T  Add track,  Ctrl+D  Duplicate track\n"
            "Ctrl+Z / Ctrl+Y  Undo / redo\n\n"
            "VIEW\n"
            "B  Preview blur,  H  Outlines\n"
            "= / -  Timeline zoom,  Ctrl+0  Fit\n\n"
            "Ctrl+S  Save,  Ctrl+E  Export"))

    def show_about(self):
        QMessageBox.about(self, "About KeyBlur",
                          f"KeyBlur {__version__}\nKeyframe-based video blur tool.\n\n"
                          "PySide6 · PyAV · OpenCV · ffmpeg")

    def closeEvent(self, e):
        if self._export_worker is not None:
            if QMessageBox.question(self, "Export running", "An export is running. Cancel it and quit?") \
                    != QMessageBox.Yes:
                e.ignore()
                return
            self._export_worker.cancel()
            self._export_worker.wait(10000)
        if not self._confirm_discard():
            e.ignore()
            return
        self._stop_playback()
        for t in (self._seek_timer, self._render_timer, self._play_timer, self._autosave_timer):
            t.stop()
        self._remove_autosave()
        if self.reader:
            self.reader.close()
            self.reader = None
        self.settings.setValue("geometry", self.saveGeometry())
        e.accept()

    # ---- autosave / recovery -------------------------------------------------------
    @staticmethod
    def autosave_path() -> str:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
        d = os.path.join(base, "KeyBlur", "autosave")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "autosave.keyblur")

    def _autosave(self):
        if not self.doc.has_video or self.doc.stack.isClean():
            return
        try:
            self.doc.project.save(self.autosave_path(), relative_paths=False, extra={
                "autosave_of": self.doc.path,
                "autosave_time": datetime.now().isoformat(timespec="seconds"),
            })
        except OSError:
            pass

    def _remove_autosave(self):
        try:
            os.remove(self.autosave_path())
        except OSError:
            pass

    def _offer_recovery(self):
        p = self.autosave_path()
        if not os.path.isfile(p) or self.doc.has_video:
            return
        try:
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
            proj = Project.load(p)
        except (OSError, ValueError, ProjectError):
            self._remove_autosave()
            return
        name = os.path.basename(meta.get("autosave_of") or "") or "an unsaved project"
        r = QMessageBox.question(
            self, "Recover unsaved work?",
            f"KeyBlur closed without saving {name}\n(autosaved {meta.get('autosave_time', '?')}).\n\n"
            "Restore it?", QMessageBox.Yes | QMessageBox.No)
        if r != QMessageBox.Yes or proj.source is None:
            self._remove_autosave()
            return
        self._prepare_and_load(proj.source.path, proj.source.title_set, project=proj,
                               project_path=meta.get("autosave_of"), dirty=True)

    # ---- drag & drop -----------------------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        path = paths[0]
        e.acceptProposedAction()
        if not self._confirm_discard():
            return
        if path.lower().endswith(".keyblur"):
            self.open_project(path)
        else:
            self._remember_dir(path)
            self.open_video(path)
