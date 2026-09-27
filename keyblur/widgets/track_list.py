"""Right-panel track list: visibility / sticky toggles, rename, reorder, add/delete."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..commands import DeleteTrack, Document, ReorderTracks, SetTrackProperty
from .canvas import track_color

COL_NAME, COL_VIS, COL_STICKY = 0, 1, 2
ID_ROLE = Qt.UserRole + 1


class _Tree(QTreeWidget):
    orderChanged = Signal(list)

    def dropEvent(self, e):
        super().dropEvent(e)
        order = [self.topLevelItem(i).data(COL_NAME, ID_ROLE) for i in range(self.topLevelItemCount())]
        self.orderChanged.emit(order)


class TrackList(QWidget):
    def __init__(self, doc: Document, parent=None):
        super().__init__(parent)
        self.doc = doc
        self._updating = False

        self.tree = _Tree()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Track", "Visible", "Sticky"])
        self.tree.setRootIsDecorated(False)
        self.tree.setDragDropMode(QAbstractItemView.InternalMove)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_VIS, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_STICKY, QHeaderView.ResizeToContents)
        self.tree.headerItem().setToolTip(COL_STICKY, "Keep the blur on from the last keyframe to the end of the video")

        self.btn_add = QPushButton("+ Add Track")
        self.btn_del = QPushButton("Delete")
        self.btn_up = QPushButton("▲")
        self.btn_down = QPushButton("▼")
        for b in (self.btn_up, self.btn_down):
            b.setFixedWidth(30)
        self.btn_up.setToolTip("Move track up (drawn below later tracks)")
        self.btn_down.setToolTip("Move track down")

        btns = QHBoxLayout()
        btns.addWidget(self.btn_add)
        btns.addWidget(self.btn_del)
        btns.addStretch()
        btns.addWidget(self.btn_up)
        btns.addWidget(self.btn_down)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tree)
        lay.addLayout(btns)

        self.btn_add.clicked.connect(self._add)
        self.btn_del.clicked.connect(self._delete)
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.currentItemChanged.connect(self._current_changed)
        self.tree.orderChanged.connect(self._reordered)
        self._rebuild_timer = QTimer(self, singleShot=True, interval=0)
        self._rebuild_timer.timeout.connect(self.rebuild)
        # deferred: rebuilding while the tree is emitting itemChanged is unsafe
        doc.projectChanged.connect(self._rebuild_timer.start)
        doc.selectionChanged.connect(self._sync_selection)
        doc.sourceChanged.connect(self.rebuild)
        self.rebuild()

    def rebuild(self):
        self._updating = True
        self.tree.clear()
        for i, tr in enumerate(self.doc.project.tracks):
            it = QTreeWidgetItem([tr.name, "", ""])
            it.setData(COL_NAME, ID_ROLE, tr.id)
            it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable
                        | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            it.setCheckState(COL_VIS, Qt.Checked if tr.visible else Qt.Unchecked)
            it.setCheckState(COL_STICKY, Qt.Checked if tr.sticky else Qt.Unchecked)
            it.setForeground(COL_NAME, track_color(i))
            it.setToolTip(COL_NAME, f"{len(tr.keyframes)} keyframe(s). Double-click to rename.")
            self.tree.addTopLevelItem(it)
        self._updating = False
        self._sync_selection()
        enabled = self.doc.has_video
        self.btn_add.setEnabled(enabled)
        has_sel = self.doc.selected_track is not None
        for b in (self.btn_del, self.btn_up, self.btn_down):
            b.setEnabled(has_sel)

    def _sync_selection(self):
        self._updating = True
        sel = self.doc.selected_track_id
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.data(COL_NAME, ID_ROLE) == sel:
                self.tree.setCurrentItem(it)
                break
        else:
            self.tree.setCurrentItem(None)
        has_sel = self.doc.selected_track is not None
        for b in (self.btn_del, self.btn_up, self.btn_down):
            b.setEnabled(has_sel)
        self._updating = False

    def _current_changed(self, cur, _prev):
        if not self._updating and cur is not None:
            self.doc.select_track(cur.data(COL_NAME, ID_ROLE))

    def _item_changed(self, it: QTreeWidgetItem, col: int):
        if self._updating:
            return
        tid = it.data(COL_NAME, ID_ROLE)
        tr = self.doc.project.track_by_id(tid)
        if tr is None:
            return
        if col == COL_NAME:
            name = it.text(COL_NAME).strip()
            if name and name != tr.name:
                self.doc.stack.push(SetTrackProperty(self.doc, tid, "name", name))
            else:
                self.rebuild()
        elif col in (COL_VIS, COL_STICKY):
            attr = "visible" if col == COL_VIS else "sticky"
            val = it.checkState(col) == Qt.Checked
            if val != getattr(tr, attr):
                self.doc.stack.push(SetTrackProperty(self.doc, tid, attr, val))

    def _add(self):
        if self.doc.has_video:
            self.doc.add_track()

    def _delete(self):
        tr = self.doc.selected_track
        if tr:
            self.doc.stack.push(DeleteTrack(self.doc, tr.id))

    def _move(self, delta: int):
        tr = self.doc.selected_track
        if not tr:
            return
        order = [t.id for t in self.doc.project.tracks]
        i = order.index(tr.id)
        j = i + delta
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
            self.doc.stack.push(ReorderTracks(self.doc, order))

    def _reordered(self, order: list):
        if order != [t.id for t in self.doc.project.tracks] and None not in order:
            self.doc.stack.push(ReorderTracks(self.doc, order))
        else:
            self.rebuild()
