"""Timeline: time ruler, one row per track with keyframe diamonds, playhead."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QMenu, QSizePolicy, QWidget

from ..commands import AddKeyframe, DeleteKeyframe, Document, EditKeyframe
from ..model import INTERPOLATIONS
from .canvas import track_color

RULER_H = 22
ROW_H = 26
LABEL_W = 110
DIAMOND = 6


def fmt_time(t: float, fps: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    frames = int(round((s - int(s)) * fps))
    if frames >= round(fps):
        frames = 0
        s = int(s) + 1
    return f"{int(m):02d}:{int(s):02d}.{frames:02d}"


class Timeline(QWidget):
    def __init__(self, doc: Document, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.view_start = 0.0
        self.view_span = 10.0
        self.setMinimumHeight(RULER_H + ROW_H * 2 + 4)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self._drag: Optional[dict] = None
        doc.projectChanged.connect(self._on_project)
        doc.selectionChanged.connect(self.update)
        doc.playheadChanged.connect(self._on_playhead)
        doc.sourceChanged.connect(self.fit_all)

    # ---- mapping --------------------------------------------------------------
    @property
    def duration(self) -> float:
        return max(self.doc.project.duration, 0.001)

    def _track_w(self) -> float:
        return max(10.0, self.width() - LABEL_W - 8)

    def t2x(self, t: float) -> float:
        return LABEL_W + (t - self.view_start) / self.view_span * self._track_w()

    def x2t(self, x: float) -> float:
        return self.view_start + (x - LABEL_W) / self._track_w() * self.view_span

    def _row_at(self, y: float) -> int:
        if y < RULER_H:
            return -1
        return int((y - RULER_H) // ROW_H)

    def _clamp_view(self):
        self.view_span = max(min(self.view_span, self.duration), 10 / float(self.doc.project.fps))
        self.view_start = max(0.0, min(self.view_start, self.duration - self.view_span))

    def fit_all(self):
        self.view_start, self.view_span = 0.0, self.duration
        self.update()

    def _on_project(self):
        n = len(self.doc.project.tracks)
        self.setMinimumHeight(RULER_H + ROW_H * max(2, n) + 4)
        self.update()

    def _on_playhead(self, _frame: int):
        t = self.doc.time
        # keep the playhead visible (page-follow)
        if t < self.view_start or t > self.view_start + self.view_span:
            self.view_start = t - self.view_span * 0.1
            self._clamp_view()
        self.update()

    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(800, RULER_H + ROW_H * max(3, len(self.doc.project.tracks)) + 4)

    # ---- painting -------------------------------------------------------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), QColor("#1e1e1e"))
        fps = float(self.doc.project.fps)
        w = self.width()

        # ruler
        p.fillRect(QRectF(0, 0, w, RULER_H), QColor("#2a2a2a"))
        if self.doc.has_video:
            step = self._tick_step()
            t = (self.view_start // step) * step
            p.setPen(QColor("#aaa"))
            f = p.font()
            f.setPointSize(8)
            p.setFont(f)
            while t <= self.view_start + self.view_span + step:
                x = self.t2x(t)
                if x >= LABEL_W:
                    p.drawLine(QPointF(x, RULER_H - 7), QPointF(x, RULER_H))
                    p.drawText(QPointF(x + 3, RULER_H - 9), fmt_time(t, fps))
                t += step

        # rows
        sel = self.doc.selected_track_id
        cur_t = self.doc.time
        for i, tr in enumerate(self.doc.project.tracks):
            y = RULER_H + i * ROW_H
            row = QRectF(0, y, w, ROW_H)
            p.fillRect(row, QColor("#2d3a4a") if tr.id == sel else QColor("#242424" if i % 2 else "#202020"))
            col = track_color(i)
            if not tr.visible:
                col.setAlpha(90)
            p.setPen(col if tr.visible else QColor("#777"))
            p.drawText(QRectF(6, y, LABEL_W - 10, ROW_H), Qt.AlignVCenter | Qt.AlignLeft,
                       tr.name + ("" if tr.visible else " (hidden)"))
            rng = tr.active_range(self.duration)
            if rng:
                x0, x1 = self.t2x(rng[0]), self.t2x(rng[1])
                bar_col = QColor(col)
                bar_col.setAlpha(70)
                p.fillRect(QRectF(x0, y + 8, max(2.0, x1 - x0), ROW_H - 16), bar_col)
                if tr.sticky and tr.keyframes:
                    xs = self.t2x(tr.keyframes[-1].time)
                    pen = QPen(col, 1, Qt.DashLine)
                    p.setPen(pen)
                    p.drawLine(QPointF(xs, y + ROW_H / 2), QPointF(x1, y + ROW_H / 2))
            for k in tr.keyframes:
                x = self.t2x(k.time)
                if x < LABEL_W - DIAMOND or x > w + DIAMOND:
                    continue
                at_head = abs(k.time - cur_t) < 1e-4
                self._draw_key(p, QPointF(x, y + ROW_H / 2), k.interpolation, col,
                               highlight=(tr.id == sel and at_head))

        # label column separator
        p.setPen(QColor("#444"))
        p.drawLine(QPointF(LABEL_W, 0), QPointF(LABEL_W, self.height()))

        # playhead
        if self.doc.has_video:
            x = self.t2x(cur_t)
            if x >= LABEL_W:
                p.setPen(QPen(QColor("#ff5252"), 1.5))
                p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
                p.setBrush(QColor("#ff5252"))
                p.drawPolygon(QPolygonF([QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x, 7)]))
        else:
            p.setPen(QColor("#777"))
            p.drawText(self.rect(), Qt.AlignCenter, "Open a video to start (File > Open Video)")
        p.end()

    def _draw_key(self, p: QPainter, c: QPointF, interp: str, col: QColor, highlight: bool):
        d = DIAMOND + (2 if highlight else 0)
        p.setPen(QPen(QColor("white") if highlight else QColor("black"), 1.5 if highlight else 1))
        p.setBrush(col)
        if interp == "hold":
            p.drawRect(QRectF(c.x() - d * 0.8, c.y() - d * 0.8, d * 1.6, d * 1.6))
        elif interp == "ease":
            path = QPainterPath()
            path.addRoundedRect(QRectF(c.x() - d, c.y() - d * 0.8, d * 2, d * 1.6), d * 0.8, d * 0.8)
            p.drawPath(path)
        else:
            p.drawPolygon(QPolygonF([QPointF(c.x(), c.y() - d), QPointF(c.x() + d, c.y()),
                                     QPointF(c.x(), c.y() + d), QPointF(c.x() - d, c.y())]))

    def _tick_step(self) -> float:
        px_per_sec = self._track_w() / self.view_span
        fps = float(self.doc.project.fps)
        for s in (1 / fps, 5 / fps, 10 / fps, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800):
            if s * px_per_sec >= 70:
                return s
        return 3600

    # ---- hit testing ------------------------------------------------------------
    def _key_at(self, pos: QPointF):
        row = self._row_at(pos.y())
        tracks = self.doc.project.tracks
        if not (0 <= row < len(tracks)):
            return None, None
        tr = tracks[row]
        best, best_d = None, DIAMOND + 3
        for k in tr.keyframes:
            dist = abs(self.t2x(k.time) - pos.x())
            if dist <= best_d:
                best, best_d = k, dist
        return tr, best

    def _seek_x(self, x: float):
        t = max(0.0, min(self.x2t(x), self.duration))
        self.doc.set_frame(self.doc.project.frame_of(t))

    # ---- mouse --------------------------------------------------------------
    def mousePressEvent(self, e):
        if not self.doc.has_video:
            return
        pos = e.position()
        if e.button() == Qt.RightButton:
            self._context_menu(pos, e.globalPosition().toPoint())
            return
        if e.button() != Qt.LeftButton:
            return
        row = self._row_at(pos.y())
        tracks = self.doc.project.tracks
        if 0 <= row < len(tracks):
            self.doc.select_track(tracks[row].id)
        if pos.x() < LABEL_W:
            return
        tr, k = self._key_at(pos)
        if k is not None:
            self.doc.set_frame(self.doc.project.frame_of(k.time))
            self._drag = {"mode": "key", "track_id": tr.id, "kf_id": k.id, "moved": False}
        else:
            self._seek_x(pos.x())
            self._drag = {"mode": "scrub"}

    def mouseMoveEvent(self, e):
        if self._drag is None:
            _tr, k = self._key_at(e.position()) if self.doc.has_video else (None, None)
            self.setCursor(Qt.SizeHorCursor if k is not None else Qt.ArrowCursor)
            return
        x = e.position().x()
        if self._drag["mode"] == "scrub":
            self._seek_x(x)
            return
        tr = self.doc.project.track_by_id(self._drag["track_id"])
        k = tr.keyframe_by_id(self._drag["kf_id"]) if tr else None
        if k is None:
            return
        proj = self.doc.project
        one = proj.time_of(1)
        idx = tr.keyframes.index(k)
        lo = tr.keyframes[idx - 1].time + one if idx > 0 else 0.0
        hi = tr.keyframes[idx + 1].time - one if idx + 1 < len(tr.keyframes) else proj.duration
        t = proj.snap(min(max(self.x2t(x), lo), hi))
        if abs(t - k.time) > 1e-6:
            self._drag["moved"] = True
            self.doc.stack.push(EditKeyframe(self.doc, tr.id, k.id, {"time": t},
                                             merge_key=f"retime-{id(self._drag)}"))
            self.doc.set_frame(proj.frame_of(t))

    def mouseReleaseEvent(self, _e):
        self._drag = None

    def wheelEvent(self, e):
        if not self.doc.has_video:
            return
        delta = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.ControlModifier:
            anchor = self.x2t(e.position().x())
            factor = 0.8 if delta > 0 else 1.25
            self.view_span *= factor
            self._clamp_view()
            self.view_start = anchor - (e.position().x() - LABEL_W) / self._track_w() * self.view_span
        else:
            self.view_start -= (delta / 120) * self.view_span * 0.1
        self._clamp_view()
        self.update()

    def _context_menu(self, pos: QPointF, gpos):
        row = self._row_at(pos.y())
        tracks = self.doc.project.tracks
        if not (0 <= row < len(tracks)) or pos.x() < LABEL_W:
            return
        tr = tracks[row]
        self.doc.select_track(tr.id)
        _tr, k = self._key_at(pos)
        menu = QMenu(self)
        if k is None:
            t = self.doc.project.snap(self.x2t(pos.x()))
            act = menu.addAction(f"Add keyframe here ({fmt_time(t, float(self.doc.project.fps))})")

            def add():
                self.doc.set_frame(self.doc.project.frame_of(t))
                self.doc.stack.push(AddKeyframe(self.doc, tr.id, self.doc.new_keyframe_for(tr, t)))
            act.triggered.connect(add)
        else:
            self.doc.set_frame(self.doc.project.frame_of(k.time))
            menu.addAction("Delete keyframe").triggered.connect(
                lambda: self.doc.stack.push(DeleteKeyframe(self.doc, tr.id, k.id)))
            sub = menu.addMenu("Interpolation to next")
            for mode in INTERPOLATIONS:
                a = QAction(mode.capitalize(), sub, checkable=True, checked=(k.interpolation == mode))
                a.triggered.connect(lambda _c=False, m=mode: self.doc.stack.push(
                    EditKeyframe(self.doc, tr.id, k.id, {"interpolation": m})))
                sub.addAction(a)
        menu.addSeparator()
        menu.addAction("Zoom to fit").triggered.connect(self.fit_all)
        menu.exec(gpos)
