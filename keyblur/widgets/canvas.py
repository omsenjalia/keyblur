"""Video preview canvas with draggable / resizable blur regions."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView

from ..commands import Document
from ..model import BlurState

HANDLE_PX = 9  # handle size in screen pixels
# handle id -> (sx, sy) position on the bounding box, relative to center
HANDLES = {
    "nw": (-1, -1), "n": (0, -1), "ne": (1, -1), "e": (1, 0),
    "se": (1, 1), "s": (0, 1), "sw": (-1, 1), "w": (-1, 0),
}
CURSORS = {
    "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor, "ne": Qt.SizeBDiagCursor,
    "sw": Qt.SizeBDiagCursor, "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor,
    "e": Qt.SizeHorCursor, "w": Qt.SizeHorCursor,
}
TRACK_COLORS = ["#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#fff176", "#4db6ac", "#f06292"]


def track_color(index: int) -> QColor:
    return QColor(TRACK_COLORS[index % len(TRACK_COLORS)])


def ndarray_to_qimage(arr) -> QImage:
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, arr.strides[0], QImage.Format_BGR888)
    return img.copy()  # detach from numpy buffer


class VideoCanvas(QGraphicsView):
    """Shows the (blurred) proxy frame and overlays one ellipse per active track."""

    def __init__(self, doc: Document, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.setScene(QGraphicsScene(self))
        self.pix = QGraphicsPixmapItem()
        self.pix.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self.pix)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor("#111"))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.show_outlines = True
        self._w, self._h = 640, 360
        self._drag: Optional[dict] = None

        doc.projectChanged.connect(self._repaint)
        doc.selectionChanged.connect(self._repaint)
        doc.playheadChanged.connect(self._repaint)

    def _repaint(self, *_):
        self.viewport().update()

    # ---- frame --------------------------------------------------------------
    def set_frame(self, arr) -> None:
        h, w = arr.shape[:2]
        self.pix.setPixmap(QPixmap.fromImage(ndarray_to_qimage(arr)))
        if (w, h) != (self._w, self._h) or self.sceneRect() != QRectF(0, 0, w, h):
            self._w, self._h = w, h
            self.setSceneRect(QRectF(0, 0, w, h))
            self._fit()
        self.viewport().update()

    def clear_frame(self) -> None:
        self.pix.setPixmap(QPixmap())
        self.viewport().update()

    def _fit(self):
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _scale(self) -> float:
        return self.transform().m11() or 1.0

    # ---- geometry helpers ---------------------------------------------------
    def _geom(self, s: BlurState) -> tuple[float, float, float, float]:
        return s.x * self._w, s.y * self._h, s.radius_x * self._w, s.radius_y * self._h

    def _regions(self) -> list[tuple[int, object, BlurState, bool]]:
        """(index, track, state, ghost) for drawable regions, selected last (on top)."""
        out = []
        sel = self.doc.selected_track_id
        for i, tr in enumerate(self.doc.project.tracks):
            if not tr.visible:
                continue
            s = tr.state_at(self.doc.time)
            if s is not None:
                out.append((i, tr, s, False))
            elif tr.id == sel:
                out.append((i, tr, self.doc.display_state(tr), True))
        out.sort(key=lambda r: r[1].id == sel)
        return out

    def _handle_rects(self, s: BlurState) -> dict[str, QRectF]:
        cx, cy, rx, ry = self._geom(s)
        hs = HANDLE_PX / self._scale()
        return {k: QRectF(cx + sx * rx - hs / 2, cy + sy * ry - hs / 2, hs, hs)
                for k, (sx, sy) in HANDLES.items()}

    def _hit(self, pos: QPointF):
        """Return (track, state, part) under the scene position; part is a handle id or 'body'."""
        regions = list(reversed(self._regions()))  # topmost first
        sel = self.doc.selected_track_id
        for _i, tr, s, _g in regions:  # handles of selected track take priority
            if tr.id == sel:
                for k, r in self._handle_rects(s).items():
                    if r.adjusted(-2, -2, 2, 2).contains(pos):
                        return tr, s, k
        for _i, tr, s, _g in regions:
            cx, cy, rx, ry = self._geom(s)
            if rx > 0 and ry > 0 and ((pos.x() - cx) / rx) ** 2 + ((pos.y() - cy) / ry) ** 2 <= 1.0:
                return tr, s, "body"
        return None

    # ---- painting -----------------------------------------------------------
    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        if not self.doc.has_video or not self.show_outlines:
            return
        painter.setRenderHint(QPainter.Antialiasing)
        scale = self._scale()
        sel = self.doc.selected_track_id
        for i, tr, s, ghost in self._regions():
            cx, cy, rx, ry = self._geom(s)
            col = track_color(i)
            selected = tr.id == sel
            pen = QPen(col, (2.5 if selected else 1.5) / scale)
            if ghost:
                pen.setStyle(Qt.DashLine)
                col.setAlpha(160)
                pen.setColor(col)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(cx, cy), rx, ry)
            if selected:
                painter.setPen(QPen(QColor(255, 255, 255, 110), 1 / scale, Qt.DotLine))
                painter.drawRect(QRectF(cx - rx, cy - ry, 2 * rx, 2 * ry))
                painter.setPen(QPen(Qt.black, 1 / scale))
                painter.setBrush(QBrush(col))
                for r in self._handle_rects(s).values():
                    painter.drawRect(r)
                # center cross
                c = 5 / scale
                painter.setPen(QPen(col, 1.5 / scale))
                painter.drawLine(QPointF(cx - c, cy), QPointF(cx + c, cy))
                painter.drawLine(QPointF(cx, cy - c), QPointF(cx, cy + c))
            # label
            painter.setPen(QPen(col))
            f = painter.font()
            f.setPointSizeF(max(1.0, 9 / scale))
            painter.setFont(f)
            label = tr.name + ("  (inactive here - drag to key)" if ghost else "")
            painter.drawText(QPointF(cx - rx, cy - ry - 4 / scale), label)

    # ---- mouse --------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.doc.has_video:
            return super().mousePressEvent(e)
        pos = self.mapToScene(e.position().toPoint())
        hit = self._hit(pos)
        if hit is None:
            return
        tr, s, part = hit
        self.doc.select_track(tr.id)
        self._drag = {"track_id": tr.id, "part": part, "start": pos, "state": s, "moved": False}
        e.accept()

    def mouseMoveEvent(self, e):
        pos = self.mapToScene(e.position().toPoint())
        if self._drag is None:
            hit = self._hit(pos) if self.doc.has_video else None
            if hit is None:
                self.viewport().unsetCursor()
            elif hit[2] == "body":
                self.viewport().setCursor(Qt.SizeAllCursor)
            else:
                self.viewport().setCursor(CURSORS[hit[2]])
            return
        d = self._drag
        tr = self.doc.project.track_by_id(d["track_id"])
        if tr is None:
            self._drag = None
            return
        if not d["moved"]:
            if (pos - d["start"]).manhattanLength() * self._scale() < 2:
                return
            d["moved"] = True
            self.doc.begin_gesture("Move blur" if d["part"] == "body" else "Resize blur")
        s0: BlurState = d["state"]
        W, H = self._w, self._h
        if d["part"] == "body":
            dx = (pos.x() - d["start"].x()) / W
            dy = (pos.y() - d["start"].y()) / H
            changes = {"x": min(1.0, max(0.0, s0.x + dx)), "y": min(1.0, max(0.0, s0.y + dy))}
        else:
            sx, sy = HANDLES[d["part"]]
            cx, cy = s0.x * W, s0.y * H
            rx_px = abs(pos.x() - cx) if sx else s0.radius_x * W
            ry_px = abs(pos.y() - cy) if sy else s0.radius_y * H
            if tr.lock_aspect:
                if sx and sy:
                    r = max(rx_px, ry_px)
                elif sx:
                    r = rx_px
                else:
                    r = ry_px
                rx_px = ry_px = r  # proxy is display-aspect-correct: equal px = visual circle
            changes = {"radius_x": max(2.0, rx_px) / W, "radius_y": max(2.0, ry_px) / H}
        self.doc.edit_at_playhead(tr, changes, merge_key="canvas")

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            if self._drag["moved"]:
                self.doc.end_gesture()
            self._drag = None
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def wheelEvent(self, e):
        """Alt+wheel over the selected region adjusts blur strength."""
        if e.modifiers() & Qt.AltModifier and self.doc.selected_track:
            tr = self.doc.selected_track
            s = self.doc.display_state(tr)
            delta = e.angleDelta().y() or e.angleDelta().x()
            step = 1 if delta > 0 else -1
            self.doc.edit_at_playhead(tr, {"strength": max(0.0, min(200.0, s.strength + step))},
                                      merge_key="wheel")
            e.accept()
            return
        super().wheelEvent(e)
