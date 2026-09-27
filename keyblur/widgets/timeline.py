"""Timeline: precise time ruler, In/Out range, one row per track with blur segments
and keyframes, snapping, hover read-out, zoom and scrolling."""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollBar,
                               QSizePolicy, QToolTip, QVBoxLayout, QWidget)

from ..commands import AddKeyframe, DeleteKeyframe, Document, EditKeyframe
from ..model import INTERPOLATION_LABELS, INTERPOLATIONS, fmt_time
from .canvas import track_color

RULER_H = 46
ROW_H = 30
LABEL_W = 130
DIAMOND = 6
SNAP_PX = 8
MAJOR_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]

C_BG = QColor("#1b1b1d")
C_RULER = QColor("#26262a")
C_TEXT = QColor("#c8c8c8")
C_DIM = QColor("#7a7a80")
C_GRID = QColor(255, 255, 255, 18)
C_GRID_SEC = QColor(255, 255, 255, 34)
C_PLAYHEAD = QColor("#ff4d4d")
C_RANGE = QColor("#4a90e2")
C_SNAP = QColor("#ffd54f")


def _label(t: float, decimals: int, long: bool) -> str:
    t = max(0.0, t)
    whole = int(t + 1e-9)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    base = f"{h}:{m:02d}:{s:02d}" if long else f"{m}:{s:02d}"
    if decimals:
        frac = int(round((t - whole) * 10 ** decimals))
        if frac >= 10 ** decimals:
            return _label(whole + 1, decimals, long)
        base += f".{frac:0{decimals}d}"
    return base


class TimelineView(QWidget):
    viewChanged = Signal()

    def __init__(self, doc: Document, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.view_start = 0.0
        self.view_span = 10.0
        self.magnet = True
        self.setMinimumHeight(RULER_H + ROW_H * 3 + 2)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self._drag: Optional[dict] = None
        self._hover_x: Optional[float] = None
        self._snap_t: Optional[float] = None
        doc.projectChanged.connect(self._on_project)
        doc.selectionChanged.connect(self.update)
        doc.playheadChanged.connect(self._on_playhead)
        doc.sourceChanged.connect(self.fit_all)
        doc.rangeChanged.connect(self.update)

    # ---- mapping ----------------------------------------------------------------
    @property
    def duration(self) -> float:
        return max(self.doc.project.duration, 0.001)

    @property
    def fps(self) -> float:
        return float(self.doc.project.fps)

    def _track_w(self) -> float:
        return max(10.0, self.width() - LABEL_W - 10)

    def pps(self) -> float:
        return self._track_w() / self.view_span

    def t2x(self, t: float) -> float:
        return LABEL_W + (t - self.view_start) * self.pps()

    def x2t(self, x: float) -> float:
        return self.view_start + (x - LABEL_W) / self.pps()

    def _row_at(self, y: float) -> int:
        return -1 if y < RULER_H else int((y - RULER_H) // ROW_H)

    def min_span(self) -> float:
        return 12 / self.fps  # ~12 frames across the whole width

    def _clamp_view(self):
        self.view_span = min(max(self.view_span, self.min_span()), self.duration)
        self.view_start = max(0.0, min(self.view_start, self.duration - self.view_span))
        self.viewChanged.emit()

    def fit_all(self):
        self.view_start, self.view_span = 0.0, self.duration
        self._clamp_view()
        self.update()

    def zoom(self, factor: float, anchor_t: Optional[float] = None):
        if anchor_t is None:
            anchor_t = self.doc.time if self.view_start <= self.doc.time <= self.view_start + self.view_span \
                else self.view_start + self.view_span / 2
        rel = (anchor_t - self.view_start) / self.view_span
        self.view_span *= factor
        self.view_span = min(max(self.view_span, self.min_span()), self.duration)
        self.view_start = anchor_t - rel * self.view_span
        self._clamp_view()
        self.update()

    def zoom_to_range(self):
        rt = self.doc.range_times()
        if rt is None:
            return
        pad = (rt[1] - rt[0]) * 0.15 + 2 / self.fps
        self.view_start, self.view_span = rt[0] - pad, rt[1] - rt[0] + 2 * pad
        self._clamp_view()
        self.update()

    def set_view_start(self, t: float):
        self.view_start = t
        self._clamp_view()
        self.update()

    def _on_project(self):
        self.setMinimumHeight(RULER_H + ROW_H * max(3, len(self.doc.project.tracks)) + 2)
        self.update()

    def _on_playhead(self, _frame: int):
        t = self.doc.time
        if self._drag is None and (t < self.view_start or t > self.view_start + self.view_span):
            self.view_start = t - self.view_span * 0.1
            self._clamp_view()
        self.update()

    def sizeHint(self):
        return QSize(800, RULER_H + ROW_H * max(3, len(self.doc.project.tracks)) + 2)

    # ---- snapping ---------------------------------------------------------------
    def snap(self, t: float, use_magnet: bool, exclude_kf: Optional[str] = None,
             include_playhead: bool = True) -> float:
        """Snap to frames; with the magnet also to whole seconds, keys, playhead, In/Out."""
        self._snap_t = None
        t = max(0.0, min(t, self.duration))
        if use_magnet:
            cands = [round(t)]
            if 0.1 * self.pps() >= 24:
                cands.append(round(t * 10) / 10)
            for tr in self.doc.project.tracks:
                cands += [k.time for k in tr.keyframes if k.id != exclude_kf]
            if include_playhead:
                cands.append(self.doc.time)
            rt = self.doc.range_times()
            if rt:
                cands += list(rt)
            best, best_px = None, SNAP_PX
            for c in cands:
                d = abs(c - t) * self.pps()
                if d <= best_px:
                    best, best_px = c, d
            if best is not None:
                t = best
                self._snap_t = self.doc.project.snap(best)
        return self.doc.project.snap(t)

    def _magnet_for(self, e) -> bool:
        return self.magnet != bool(e.modifiers() & Qt.ShiftModifier) if self._drag and \
            self._drag.get("mode") != "range" else self.magnet

    # ---- painting ---------------------------------------------------------------
    def _ticks(self):
        pps = self.pps()
        major = next((s for s in MAJOR_STEPS if s * pps >= 85), 3600)
        minor = None
        for div in (10, 5, 4, 2):
            if major / div * pps >= 9:
                minor = major / div
                break
        decimals = 1 if major < 1 else 0
        return major, minor, decimals

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), C_BG)
        if not self.doc.has_video:
            p.setPen(C_DIM)
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Open a video to start  (File > Open Video, or drag a file onto the window)")
            p.end()
            return
        small = QFont(self.font())
        small.setPointSize(8)
        p.setFont(small)
        long = self.duration >= 3600
        x_end = w
        t_end = self.view_start + self.view_span

        # rows background
        sel = self.doc.selected_track_id
        tracks = self.doc.project.tracks
        for i, tr in enumerate(tracks):
            y = RULER_H + i * ROW_H
            p.fillRect(QRectF(0, y, w, ROW_H),
                       QColor("#253447") if tr.id == sel else QColor("#212124" if i % 2 else "#1e1e21"))

        # grid lines
        major, minor, decimals = self._ticks()
        t = math.floor(self.view_start / major) * major
        while t <= t_end + major:
            x = self.t2x(t)
            if x >= LABEL_W:
                whole_sec = abs(t - round(t)) < 1e-6
                p.setPen(QPen(C_GRID_SEC if (whole_sec and major < 1) or major >= 1 else C_GRID, 1))
                p.drawLine(QPointF(x, RULER_H), QPointF(x, h))
            t += major

        # In/Out range shading
        rt = self.doc.range_times()
        if rt:
            xa, xb = max(LABEL_W, self.t2x(rt[0])), min(x_end, self.t2x(rt[1]))
            if xb > xa:
                shade = QColor(C_RANGE)
                shade.setAlpha(38)
                p.fillRect(QRectF(xa, RULER_H, xb - xa, h - RULER_H), shade)

        # track rows: segments + keyframes
        cur_t = self.doc.time
        for i, tr in enumerate(tracks):
            y = RULER_H + i * ROW_H
            col = track_color(i)
            # label column
            p.fillRect(QRectF(0, y, LABEL_W, ROW_H), QColor("#29292d") if tr.id == sel else QColor("#232326"))
            p.fillRect(QRectF(0, y + 3, 4, ROW_H - 6), col)
            name_font = QFont(small)
            name_font.setPointSize(9)
            name_font.setBold(tr.id == sel)
            name_font.setItalic(not tr.visible)
            p.setFont(name_font)
            p.setPen(C_TEXT if tr.visible else C_DIM)
            p.drawText(QRectF(10, y, LABEL_W - 14, ROW_H), Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(tr.name + ("" if tr.visible else " (hidden)"),
                                                  Qt.ElideRight, LABEL_W - 16))
            p.setFont(small)
            p.save()
            p.setClipRect(QRectF(LABEL_W, y, w - LABEL_W, ROW_H))
            fill = QColor(col)
            fill.setAlpha(115 if tr.visible else 40)
            border = QColor(col)
            border.setAlpha(220 if tr.visible else 80)
            for a, b in tr.active_segments(self.duration):
                xa, xb = self.t2x(a), self.t2x(b)
                r = QRectF(xa, y + 7, max(3.0, xb - xa), ROW_H - 14)
                p.setPen(QPen(border, 1))
                p.setBrush(fill)
                p.drawRoundedRect(r, 3, 3)
                if xb - xa > 70:
                    p.setPen(QColor(255, 255, 255, 200))
                    p.drawText(r.adjusted(12, 0, -8, 0), Qt.AlignVCenter | Qt.AlignLeft,
                               f"{b - a:.2f}s")
            if tr.sticky and tr.keyframes and tr.keyframes[-1].interpolation != "off":
                xs = self.t2x(tr.keyframes[-1].time)
                p.setPen(QPen(QColor(255, 255, 255, 150), 1, Qt.DashLine))
                p.drawLine(QPointF(xs, y + ROW_H / 2), QPointF(self.t2x(self.duration), y + ROW_H / 2))
            for k in tr.keyframes:
                x = self.t2x(k.time)
                at_head = abs(k.time - cur_t) < 1e-4
                self._draw_key(p, QPointF(x, y + ROW_H / 2), k.interpolation, col,
                               highlight=(tr.id == sel and at_head))
            p.restore()

        # ruler (drawn after rows so labels stay crisp)
        p.fillRect(QRectF(0, 0, w, RULER_H), C_RULER)
        p.fillRect(QRectF(0, 0, LABEL_W, RULER_H), QColor("#202023"))
        if rt:
            xa, xb = max(LABEL_W, self.t2x(rt[0])), min(x_end, self.t2x(rt[1]))
            if xb > xa:
                band = QColor(C_RANGE)
                band.setAlpha(150)
                p.fillRect(QRectF(xa, RULER_H - 9, xb - xa, 9), band)
            p.setPen(QPen(C_RANGE.lighter(130), 2))
            for tt, sign in ((rt[0], 1), (rt[1], -1)):
                x = self.t2x(tt)
                if LABEL_W <= x <= x_end:
                    p.drawLine(QPointF(x, 0), QPointF(x, h))
                    p.drawLine(QPointF(x, 1), QPointF(x + 6 * sign, 1))
                    p.drawLine(QPointF(x, h - 1), QPointF(x + 6 * sign, h - 1))
        # frame ticks
        fpx = self.pps() / self.fps
        if fpx >= 6:
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            f0 = int(self.view_start * self.fps)
            f1 = int(t_end * self.fps) + 1
            for f in range(f0, f1 + 1):
                x = self.t2x(f / self.fps)
                if x >= LABEL_W:
                    p.drawLine(QPointF(x, RULER_H - 3), QPointF(x, RULER_H))
        # minor/major ticks
        if minor:
            p.setPen(QPen(QColor("#6c6c72"), 1))
            t = math.floor(self.view_start / minor) * minor
            while t <= t_end + minor:
                x = self.t2x(t)
                if x >= LABEL_W:
                    p.drawLine(QPointF(x, RULER_H - 6), QPointF(x, RULER_H))
                t += minor
        t = math.floor(self.view_start / major) * major
        while t <= t_end + major:
            x = self.t2x(t)
            if x >= LABEL_W:
                p.setPen(QPen(QColor("#a9a9b0"), 1))
                p.drawLine(QPointF(x, RULER_H - 13), QPointF(x, RULER_H))
                p.setPen(C_TEXT)
                p.drawText(QPointF(x + 3, RULER_H - 16), _label(t, decimals, long))
            t += major
        p.setPen(C_DIM)
        p.drawText(QRectF(8, 0, LABEL_W - 10, RULER_H), Qt.AlignVCenter | Qt.AlignLeft,
                   f"{self.fps:.3f} fps")

        # label column separator
        p.setPen(QColor("#3a3a3f"))
        p.drawLine(QPointF(LABEL_W, 0), QPointF(LABEL_W, h))

        # snap guide
        if self._snap_t is not None and self._drag:
            x = self.t2x(self._snap_t)
            p.setPen(QPen(C_SNAP, 1, Qt.DashLine))
            p.drawLine(QPointF(x, 0), QPointF(x, h))

        # hover line + bubble
        if self._hover_x is not None and self._drag is None and self._hover_x >= LABEL_W:
            ht = self.doc.project.snap(self.x2t(self._hover_x))
            hx = self.t2x(ht)
            p.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.DotLine))
            p.drawLine(QPointF(hx, RULER_H), QPointF(hx, h))
            self._bubble(p, hx, f"{fmt_time(ht)}  f{self.doc.project.frame_of(ht)}",
                         QColor("#44444a"), y=RULER_H + 2)

        # playhead
        x = self.t2x(cur_t)
        if LABEL_W <= x <= x_end:
            p.setPen(QPen(C_PLAYHEAD, 1.5))
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            p.setBrush(C_PLAYHEAD)
            p.setPen(Qt.NoPen)
            p.drawPolygon(QPolygonF([QPointF(x - 6, RULER_H - 10), QPointF(x + 6, RULER_H - 10),
                                     QPointF(x, RULER_H - 2)]))
            self._bubble(p, x, fmt_time(cur_t), C_PLAYHEAD, y=1)
        p.end()

    def _bubble(self, p: QPainter, x: float, text: str, bg: QColor, y: float):
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text) + 10
        bx = min(max(LABEL_W + 1, x - tw / 2), self.width() - tw - 1)
        r = QRectF(bx, y, tw, fm.height() + 2)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 3, 3)
        p.setPen(QColor("white"))
        p.drawText(r, Qt.AlignCenter, text)

    def _draw_key(self, p: QPainter, c: QPointF, interp: str, col: QColor, highlight: bool):
        d = DIAMOND + (2 if highlight else 0)
        p.setPen(QPen(QColor("white") if highlight else QColor("black"), 2 if highlight else 1))
        p.setBrush(col)
        if interp == "off":
            # "]" end bracket: blur stops here
            path = QPainterPath()
            path.moveTo(c.x() - d * 0.9, c.y() - d * 1.3)
            path.lineTo(c.x() + 1.5, c.y() - d * 1.3)
            path.lineTo(c.x() + 1.5, c.y() + d * 1.3)
            path.lineTo(c.x() - d * 0.9, c.y() + d * 1.3)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("black"), 5 if highlight else 4))
            p.drawPath(path)
            p.setPen(QPen(QColor("white") if highlight else col.lighter(140), 2.5))
            p.drawPath(path)
        elif interp == "hold":
            p.drawRect(QRectF(c.x() - d * 0.8, c.y() - d * 0.8, d * 1.6, d * 1.6))
        elif interp == "ease":
            p.drawEllipse(c, d * 0.95, d * 0.95)
        else:
            p.drawPolygon(QPolygonF([QPointF(c.x(), c.y() - d), QPointF(c.x() + d, c.y()),
                                     QPointF(c.x(), c.y() + d), QPointF(c.x() - d, c.y())]))

    # ---- hit testing --------------------------------------------------------------
    def _key_at(self, pos: QPointF):
        row = self._row_at(pos.y())
        tracks = self.doc.project.tracks
        if not (0 <= row < len(tracks)):
            return None, None
        tr = tracks[row]
        best, best_d = None, DIAMOND + 4
        for k in tr.keyframes:
            dist = abs(self.t2x(k.time) - pos.x())
            if dist <= best_d:
                best, best_d = k, dist
        return tr, best

    def _range_handle_at(self, x: float) -> Optional[str]:
        rt = self.doc.range_times()
        if not rt:
            return None
        for name, tt in (("in", rt[0]), ("out", rt[1])):
            if abs(self.t2x(tt) - x) <= 5:
                return name
        return None

    def _seek(self, t: float):
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
        t_raw = self.x2t(pos.x())
        if e.modifiers() & Qt.ShiftModifier:
            f = self.doc.project.frame_of(self.snap(t_raw, self.magnet))
            self._drag = {"mode": "range", "anchor": f}
            return
        handle = self._range_handle_at(pos.x())
        if handle and pos.y() < RULER_H:
            self._drag = {"mode": handle}
            return
        tr, k = self._key_at(pos)
        if k is not None:
            self._seek(k.time)
            self._drag = {"mode": "key", "track_id": tr.id, "kf_id": k.id,
                          "offset": t_raw - k.time}
            self._drag["merge"] = f"retime-{id(self._drag)}"
        else:
            self._drag = {"mode": "scrub"}
            self._seek(self.snap(t_raw, self.magnet, include_playhead=False))
        self.update()

    def mouseDoubleClickEvent(self, e):
        if not self.doc.has_video or e.button() != Qt.LeftButton:
            return
        pos = e.position()
        row = self._row_at(pos.y())
        tracks = self.doc.project.tracks
        if pos.x() < LABEL_W or not (0 <= row < len(tracks)):
            return
        tr, k = self._key_at(pos)
        if k is None:
            t = self.snap(self.x2t(pos.x()), self.magnet)
            self._seek(t)
            if tr.keyframe_at(t) is None:
                self.doc.stack.push(AddKeyframe(self.doc, tr.id, self.doc.new_keyframe_for(tr, t)))

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self._drag is None:
            self._hover_x = pos.x()
            if self.doc.has_video:
                self._hover_feedback(e)
            self.update()
            return
        mode = self._drag["mode"]
        magnet = self._magnet_for(e)
        t_raw = self.x2t(pos.x())
        if mode == "scrub":
            self._seek(self.snap(t_raw, magnet, include_playhead=False))
        elif mode == "range":
            f = self.doc.project.frame_of(self.snap(t_raw, magnet))
            if f != self._drag["anchor"]:
                self.doc.set_range(self._drag["anchor"], f)
        elif mode in ("in", "out"):
            f = self.doc.project.frame_of(self.snap(t_raw, magnet))
            if mode == "in" and f < (self.doc.out_frame or 10**9):
                self.doc.set_in(f)
            elif mode == "out" and f > (self.doc.in_frame or -1):
                self.doc.set_out(f)
        elif mode == "key":
            tr = self.doc.project.track_by_id(self._drag["track_id"])
            k = tr.keyframe_by_id(self._drag["kf_id"]) if tr else None
            if k is not None:
                t = self.snap(t_raw - self._drag["offset"], magnet, exclude_kf=k.id,
                              include_playhead=False)
                if self.doc.retime_keyframe(tr, k, t, merge_key=self._drag["merge"]):
                    self._seek(k.time)
                QToolTip.showText(e.globalPosition().toPoint(),
                                  f"{fmt_time(k.time)}  (frame {self.doc.project.frame_of(k.time)})", self)
        self._autoscroll(pos.x())
        self.update()

    def _autoscroll(self, x: float):
        if x > self.width() - 10:
            self.set_view_start(self.view_start + self.view_span * 0.02)
        elif LABEL_W <= x < LABEL_W + 10 and self.view_start > 0:
            self.set_view_start(self.view_start - self.view_span * 0.02)

    def _hover_feedback(self, e):
        pos = e.position()
        tr, k = self._key_at(pos)
        if k is not None:
            self.setCursor(Qt.SizeHorCursor)
            QToolTip.showText(e.globalPosition().toPoint(),
                              f"<b>{tr.name}</b><br>{fmt_time(k.time)} (frame {self.doc.project.frame_of(k.time)})"
                              f"<br>{INTERPOLATION_LABELS[k.interpolation]}<br><i>drag to move, "
                              f"right-click for options</i>", self)
            return
        if pos.y() < RULER_H and self._range_handle_at(pos.x()):
            self.setCursor(Qt.SplitHCursor)
            return
        self.setCursor(Qt.ArrowCursor)
        row = self._row_at(pos.y())
        tracks = self.doc.project.tracks
        if 0 <= row < len(tracks) and pos.x() >= LABEL_W:
            t = self.x2t(pos.x())
            for a, b in tracks[row].active_segments(self.duration):
                if a <= t <= b:
                    QToolTip.showText(e.globalPosition().toPoint(),
                                      f"<b>{tracks[row].name}</b>: blur {fmt_time(a)} → {fmt_time(b)}"
                                      f"<br>{b - a:.3f} s", self)
                    return
        QToolTip.hideText()

    def mouseReleaseEvent(self, _e):
        self._drag = None
        self._snap_t = None
        self.update()

    def leaveEvent(self, _e):
        self._hover_x = None
        self.update()

    def wheelEvent(self, e):
        if not self.doc.has_video:
            return
        delta = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.ControlModifier:
            self.zoom(0.8 if delta > 0 else 1.25, anchor_t=self.x2t(e.position().x()))
        else:
            self.set_view_start(self.view_start - (delta / 120) * self.view_span * 0.1)

    def _context_menu(self, pos: QPointF, gpos):
        if pos.x() < LABEL_W:
            return
        doc = self.doc
        row = self._row_at(pos.y())
        tracks = doc.project.tracks
        tr = tracks[row] if 0 <= row < len(tracks) else None
        t = self.snap(self.x2t(pos.x()), self.magnet)
        menu = QMenu(self)
        if tr is not None:
            doc.select_track(tr.id)
            _tr, k = self._key_at(pos)
            if k is not None:
                self._seek(k.time)
                menu.addSection(f"Keyframe {fmt_time(k.time)}")
                menu.addAction("Delete keyframe").triggered.connect(
                    lambda: doc.stack.push(DeleteKeyframe(doc, tr.id, k.id)))
                sub = menu.addMenu("After this keyframe")
                for mode in INTERPOLATIONS:
                    a = QAction(INTERPOLATION_LABELS[mode], sub, checkable=True,
                                checked=(k.interpolation == mode))
                    a.triggered.connect(lambda _c=False, m=mode: doc.stack.push(
                        EditKeyframe(doc, tr.id, k.id, {"interpolation": m})))
                    sub.addAction(a)
            else:
                menu.addSection(f"{tr.name} at {fmt_time(t)}")

                def at_t(fn):
                    def run():
                        self._seek(t)
                        fn()
                    return run
                menu.addAction("Add keyframe here").triggered.connect(at_t(
                    lambda: doc.stack.push(AddKeyframe(doc, tr.id, doc.new_keyframe_for(tr, t)))
                    if tr.keyframe_at(t) is None else None))
                on = tr.state_at(t) is not None
                menu.addAction("Turn blur OFF from here" if on else "Turn blur ON from here").triggered.connect(
                    at_t(lambda: doc.toggle_blur_at_playhead(tr)))
            if doc.has_range:
                menu.addSection("In → Out range")
                menu.addAction(f"Blur In→Out on {tr.name}").triggered.connect(
                    lambda: doc.blur_range(new_track=False))
                menu.addAction(f"Remove blur In→Out from {tr.name}").triggered.connect(doc.remove_blur_range)
        menu.addSection("Range")
        menu.addAction(f"Set In here ({fmt_time(t)})").triggered.connect(
            lambda: doc.set_in(doc.project.frame_of(t)))
        menu.addAction(f"Set Out here ({fmt_time(t)})").triggered.connect(
            lambda: doc.set_out(doc.project.frame_of(t)))
        if doc.has_range:
            menu.addAction("Blur In→Out as new track").triggered.connect(lambda: doc.blur_range(True))
            menu.addAction("Clear In/Out").triggered.connect(doc.clear_range)
        menu.addSeparator()
        menu.addAction("Zoom to fit").triggered.connect(self.fit_all)
        menu.exec(gpos)


class Timeline(QWidget):
    """Timeline view plus its toolbar (zoom, magnet, In/Out) and a scrollbar."""

    def __init__(self, doc: Document, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.view = TimelineView(doc)
        self.scroll = QScrollBar(Qt.Horizontal)

        def btn(text, tip, slot, w=None):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            if w:
                b.setFixedWidth(w)
            b.clicked.connect(slot)
            return b

        self.btn_zoom_out = btn("−", "Zoom out (Ctrl+wheel)", lambda: self.view.zoom(1.5), 28)
        self.btn_zoom_in = btn("+", "Zoom in (Ctrl+wheel)", lambda: self.view.zoom(1 / 1.5), 28)
        self.btn_fit = btn("Fit", "Show the whole video (Ctrl+0)", self.view.fit_all)
        self.btn_zoom_range = btn("Zoom In→Out", "Zoom to the In/Out range", self.view.zoom_to_range)
        self.chk_magnet = QCheckBox("Snap")
        self.chk_magnet.setChecked(True)
        self.chk_magnet.setFocusPolicy(Qt.NoFocus)
        self.chk_magnet.setToolTip("Snap to whole seconds, keyframes, playhead and In/Out.\n"
                                   "Hold Shift while dragging a keyframe to invert.")
        self.chk_magnet.toggled.connect(lambda v: setattr(self.view, "magnet", v))
        self.btn_in = btn("[ Set In", "Set In point at playhead (I)", lambda: doc.set_in())
        self.btn_out = btn("Set Out ]", "Set Out point at playhead (O)", lambda: doc.set_out())
        self.btn_clear = btn("Clear", "Clear In/Out (Alt+X)", doc.clear_range)
        self.range_label = QLabel()
        self.range_label.setStyleSheet("color:#8fb8ff;")
        hint = QLabel("Shift+drag: range · Double-click: key · Right-click: menu")
        hint.setStyleSheet("color:#777;")

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        for wdg in (self.btn_zoom_out, self.btn_zoom_in, self.btn_fit, self.btn_zoom_range, self.chk_magnet):
            bar.addWidget(wdg)
        bar.addSpacing(14)
        for wdg in (self.btn_in, self.btn_out, self.btn_clear, self.range_label):
            bar.addWidget(wdg)
        bar.addStretch()
        bar.addWidget(hint)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.scroll)

        self._sync = False
        self.view.viewChanged.connect(self._update_scroll)
        self.scroll.valueChanged.connect(self._scrolled)
        doc.rangeChanged.connect(self._update_range_label)
        doc.sourceChanged.connect(self._update_range_label)
        self._update_range_label()

    # convenience passthroughs
    def fit_all(self):
        self.view.fit_all()

    def t2x(self, t):
        return self.view.t2x(t)

    def _update_scroll(self):
        self._sync = True
        v = self.view
        span_ms = int(v.view_span * 1000)
        self.scroll.setRange(0, max(0, int(v.duration * 1000) - span_ms))
        self.scroll.setPageStep(max(1, span_ms))
        self.scroll.setSingleStep(max(1, span_ms // 20))
        self.scroll.setValue(int(v.view_start * 1000))
        self._sync = False

    def _scrolled(self, val: int):
        if not self._sync:
            self.view.set_view_start(val / 1000)

    def _update_range_label(self):
        doc = self.doc
        parts = []
        if doc.in_frame is not None:
            parts.append(f"In {fmt_time(doc.project.time_of(doc.in_frame))}")
        if doc.out_frame is not None:
            parts.append(f"Out {fmt_time(doc.project.time_of(doc.out_frame))}")
        rt = doc.range_times()
        if rt:
            parts.append(f"({rt[1] - rt[0]:.3f} s)")
        self.range_label.setText("   ".join(parts) if parts else "no range")
        self.btn_clear.setEnabled(bool(parts))
        self.btn_zoom_range.setEnabled(rt is not None)
