"""Keyframe inspector: numeric/slider editing of the selected track at the playhead."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget)

from ..commands import Document, EditKeyframe, SetTrackProperty
from ..model import INTERPOLATION_LABELS, INTERPOLATIONS
from .timeline import fmt_time


class FloatField(QWidget):
    """Slider + spinbox pair. Emits edited(value) on user changes and
    gestureEnded() when a slider drag / spin edit finishes."""

    edited = Signal(float)
    gestureStarted = Signal()
    gestureEnded = Signal()

    def __init__(self, lo: float, hi: float, decimals: int = 1, suffix: str = "", parent=None):
        super().__init__(parent)
        self._scale = 10 ** decimals
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(lo * self._scale), int(hi * self._scale))
        self.spin = QDoubleSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setDecimals(decimals)
        self.spin.setSuffix(suffix)
        self.spin.setKeyboardTracking(False)
        self.spin.setFixedWidth(84)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self._block = False
        self.slider.valueChanged.connect(self._slider_changed)
        self.slider.sliderPressed.connect(self.gestureStarted)
        self.slider.sliderReleased.connect(self.gestureEnded)
        self.spin.valueChanged.connect(self._spin_changed)

    def set_value(self, v: float) -> None:
        self._block = True
        self.spin.setValue(v)
        if not self.slider.isSliderDown():
            self.slider.setValue(int(round(v * self._scale)))
        self._block = False

    def value(self) -> float:
        return self.spin.value()

    def _slider_changed(self, iv: int):
        if self._block:
            return
        v = iv / self._scale
        self._block = True
        self.spin.setValue(v)
        self._block = False
        self.edited.emit(v)
        if not self.slider.isSliderDown():  # keyboard / page-step change
            self.gestureEnded.emit()

    def _spin_changed(self, v: float):
        if self._block:
            return
        self._block = True
        self.slider.setValue(int(round(v * self._scale)))
        self._block = False
        self.edited.emit(v)
        self.gestureEnded.emit()


class Inspector(QGroupBox):
    def __init__(self, doc: Document, parent=None):
        super().__init__("Keyframe Inspector", parent)
        self.doc = doc
        self._gesture_n = 0
        self._refreshing = False

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #aaa;")
        self.f_x = FloatField(0, 100, 1, " %")
        self.f_y = FloatField(0, 100, 1, " %")
        self.f_rx = FloatField(0.2, 100, 1, " %")
        self.f_ry = FloatField(0.2, 100, 1, " %")
        self.f_strength = FloatField(0, 200, 1, " px")
        self.interp = QComboBox()
        for m in INTERPOLATIONS:
            self.interp.addItem(INTERPOLATION_LABELS[m], m)
        self.time = QDoubleSpinBox()
        self.time.setDecimals(3)
        self.time.setSuffix(" s")
        self.time.setKeyboardTracking(False)
        self.time.setToolTip("Exact time of the keyframe at the playhead (snaps to the nearest frame)")
        self.frame_lbl = QLabel()
        self.frame_lbl.setStyleSheet("color:#888;")
        time_row = QWidget()
        tl = QHBoxLayout(time_row)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.time, 1)
        tl.addWidget(self.frame_lbl)
        self.lock = QCheckBox("Lock aspect (circle)")
        self.sticky = QCheckBox("Stay on until end of video")

        form = QFormLayout()
        form.addRow("Key time", time_row)
        form.addRow("X", self.f_x)
        form.addRow("Y", self.f_y)
        form.addRow("Radius X", self.f_rx)
        form.addRow("Radius Y", self.f_ry)
        form.addRow("Strength", self.f_strength)
        form.addRow("After key", self.interp)
        lay = QVBoxLayout(self)
        lay.addWidget(self.status)
        lay.addLayout(form)
        lay.addWidget(self.lock)
        lay.addWidget(self.sticky)
        lay.addStretch()

        self.fields = {"x": self.f_x, "y": self.f_y, "radius_x": self.f_rx,
                       "radius_y": self.f_ry, "strength": self.f_strength}
        for prop, fld in self.fields.items():
            fld.edited.connect(lambda v, p=prop: self._edited(p, v))
            fld.gestureStarted.connect(self._gesture_start)
            fld.gestureEnded.connect(self._gesture_end)
        self.interp.activated.connect(self._interp_changed)
        self.time.valueChanged.connect(self._time_changed)
        self.lock.toggled.connect(lambda v: self._track_flag("lock_aspect", v))
        self.sticky.toggled.connect(lambda v: self._track_flag("sticky", v))

        doc.projectChanged.connect(self.refresh)
        doc.selectionChanged.connect(self.refresh)
        doc.playheadChanged.connect(self.refresh)
        doc.sourceChanged.connect(self.refresh)
        self.refresh()

    # ---- display --------------------------------------------------------------
    def refresh(self, *_):
        self._refreshing = True
        tr = self.doc.selected_track
        enabled = tr is not None and self.doc.has_video
        for w in (*self.fields.values(), self.interp, self.lock, self.sticky, self.time):
            w.setEnabled(enabled)
        self.frame_lbl.setText("")
        if not enabled:
            self.status.setText("Select or add a track to edit its blur." if self.doc.has_video
                                else "No video loaded.")
            self._refreshing = False
            return
        s = self.doc.display_state(tr)
        self.f_x.set_value(s.x * 100)
        self.f_y.set_value(s.y * 100)
        self.f_rx.set_value(s.radius_x * 100)
        self.f_ry.set_value(s.radius_y * 100)
        self.f_strength.set_value(s.strength)
        self.lock.setChecked(tr.lock_aspect)
        self.sticky.setChecked(tr.sticky)
        k = self.doc.keyframe_at_playhead(tr)
        seg = self.doc.segment_keyframe(tr)
        if k is not None:
            off = " <span style='color:#f88'>(blur turns off)</span>" if k.interpolation == "off" else ""
            self.status.setText(f"<b>{tr.name}</b> - keyframe at {fmt_time(k.time)}{off}")
        elif tr.state_at(self.doc.time) is not None:
            self.status.setText(f"<b>{tr.name}</b> - blurring (between keys). Editing adds a keyframe here.")
        else:
            self.status.setText(f"<b>{tr.name}</b> - <span style='color:#f88'>no blur here</span>. "
                                "Editing turns it on from here.")
        self.time.setEnabled(k is not None)
        self.time.setRange(0.0, max(0.0, self.doc.project.duration))
        self.time.setValue(k.time if k is not None else self.doc.time)
        self.frame_lbl.setText(f"frame {self.doc.frame}")
        self.interp.setEnabled(seg is not None)
        if seg is not None:
            self.interp.setCurrentIndex(INTERPOLATIONS.index(seg.interpolation))
            self.interp.setToolTip(f"What happens after the keyframe at {fmt_time(seg.time)} "
                                   "until the next keyframe.")
        self._refreshing = False

    # ---- editing --------------------------------------------------------------
    def _gesture_start(self):
        self.doc.begin_gesture("Adjust blur")

    def _gesture_end(self):
        self.doc.end_gesture()
        self._gesture_n += 1

    def _edited(self, prop: str, value: float):
        if self._refreshing:
            return
        tr = self.doc.selected_track
        if tr is None:
            return
        if prop in ("x", "y", "radius_x", "radius_y"):
            value /= 100.0
        changes = {prop: value}
        if tr.lock_aspect and prop == "radius_x":
            changes["radius_y"] = min(1.0, value * self.doc.aspect)
        elif tr.lock_aspect and prop == "radius_y":
            changes["radius_x"] = min(1.0, value / self.doc.aspect)
        self.doc.edit_at_playhead(tr, changes, merge_key=f"inspector-{prop}-{self._gesture_n}")

    def _time_changed(self, v: float):
        if self._refreshing:
            return
        tr = self.doc.selected_track
        k = self.doc.keyframe_at_playhead(tr) if tr else None
        if k is None:
            return
        if self.doc.retime_keyframe(tr, k, v):
            self.doc.set_frame(self.doc.project.frame_of(k.time))
        else:
            self.refresh()

    def _interp_changed(self, _idx: int):
        tr = self.doc.selected_track
        seg = self.doc.segment_keyframe(tr) if tr else None
        mode = self.interp.currentData()
        if seg is not None and seg.interpolation != mode:
            self.doc.stack.push(EditKeyframe(self.doc, tr.id, seg.id, {"interpolation": mode}))

    def _track_flag(self, attr: str, value: bool):
        if self._refreshing:
            return
        tr = self.doc.selected_track
        if tr is None or getattr(tr, attr) == value:
            return
        if attr == "lock_aspect" and value:
            # snap to a visual circle using the current radius X
            self.doc.stack.beginMacro("Lock aspect")
            self.doc.stack.push(SetTrackProperty(self.doc, tr.id, attr, value))
            s = self.doc.display_state(tr)
            if tr.keyframes:
                self.doc.edit_at_playhead(tr, {"radius_y": min(1.0, s.radius_x * self.doc.aspect)})
            self.doc.stack.endMacro()
        else:
            self.doc.stack.push(SetTrackProperty(self.doc, tr.id, attr, value))
