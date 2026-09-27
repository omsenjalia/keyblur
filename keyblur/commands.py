"""Document (project + selection + undo stack) and undoable edit commands."""
from __future__ import annotations

import copy
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoCommand, QUndoStack

from .model import BlurState, Keyframe, Project, Track

DEFAULT_RADIUS = 0.08
DEFAULT_STRENGTH = 25.0


class Document(QObject):
    """Holds the project and UI selection; widgets observe its signals."""

    projectChanged = Signal()      # any track/keyframe data changed
    selectionChanged = Signal()    # selected track changed
    playheadChanged = Signal(int)  # current frame index
    sourceChanged = Signal()       # a new video was loaded / project replaced

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = Project()
        self.path: Optional[str] = None
        self.playable_path: Optional[str] = None
        self.stack = QUndoStack(self)
        self.selected_track_id: Optional[str] = None
        self.frame = 0
        self.default_interpolation = "linear"

    # ---- state -----------------------------------------------------------
    def reset(self, project: Project, path: Optional[str], playable: Optional[str]) -> None:
        self.project = project
        self.path = path
        self.playable_path = playable
        self.stack.clear()
        self.stack.setClean()
        self.selected_track_id = project.tracks[0].id if project.tracks else None
        self.frame = 0
        self.sourceChanged.emit()
        self.projectChanged.emit()
        self.selectionChanged.emit()
        self.playheadChanged.emit(0)

    @property
    def has_video(self) -> bool:
        return self.project.source is not None and self.playable_path is not None

    @property
    def time(self) -> float:
        return self.project.time_of(self.frame)

    @property
    def frame_count(self) -> int:
        return self.project.source.frame_count if self.project.source else 1

    def set_frame(self, frame: int) -> None:
        frame = max(0, min(frame, self.frame_count - 1))
        if frame != self.frame:
            self.frame = frame
            self.playheadChanged.emit(frame)

    def select_track(self, track_id: Optional[str]) -> None:
        if track_id != self.selected_track_id:
            self.selected_track_id = track_id
            self.selectionChanged.emit()

    @property
    def selected_track(self) -> Optional[Track]:
        if self.selected_track_id is None:
            return None
        return self.project.track_by_id(self.selected_track_id)

    def keyframe_at_playhead(self, track: Optional[Track] = None) -> Optional[Keyframe]:
        track = track or self.selected_track
        return track.keyframe_at(self.time) if track else None

    def segment_keyframe(self, track: Optional[Track] = None) -> Optional[Keyframe]:
        """Keyframe at the playhead, else the one governing the current segment."""
        track = track or self.selected_track
        if not track or not track.keyframes:
            return None
        t = self.time
        prev = [k for k in track.keyframes if k.time <= t + 1e-4]
        return prev[-1] if prev else track.keyframes[0]

    def display_state(self, track: Track) -> BlurState:
        """State to show/edit for a track at the playhead, even outside its active range."""
        s = track.state_at(self.time)
        if s is not None:
            return s
        if track.keyframes:
            t = self.time
            k = track.keyframes[0] if t < track.keyframes[0].time else track.keyframes[-1]
            return BlurState(k.x, k.y, k.radius_x, k.radius_y, k.strength, track.id)
        ry = DEFAULT_RADIUS * self.aspect
        return BlurState(0.5, 0.5, DEFAULT_RADIUS, ry, DEFAULT_STRENGTH, track.id)

    @property
    def aspect(self) -> float:
        """Display aspect: radius_y = radius_x * aspect gives a visual circle."""
        return self.project.source.display_aspect if self.project.source else 16 / 9

    # ---- high-level edit helpers ------------------------------------------
    def new_keyframe_for(self, track: Track, t: float) -> Keyframe:
        s = self.display_state(track)
        prev = [k for k in track.keyframes if k.time < t]
        interp = prev[-1].interpolation if prev else self.default_interpolation
        return Keyframe(time=t, x=s.x, y=s.y, radius_x=s.radius_x, radius_y=s.radius_y,
                        strength=s.strength, interpolation=interp)

    def ensure_keyframe_at_playhead(self, track: Track) -> Keyframe:
        """Return the keyframe at the playhead, creating one (auto-key) if needed."""
        k = track.keyframe_at(self.time)
        if k is None:
            k = self.new_keyframe_for(track, self.time)
            self.stack.push(AddKeyframe(self, track.id, k))
        return k

    def edit_at_playhead(self, track: Track, changes: dict, merge_key: Optional[str] = None) -> None:
        """Apply property changes to the keyframe at the playhead (auto-keying).

        Inside a gesture (begin_gesture/end_gesture) repeated edits with the same
        merge_key collapse into the gesture's single undo step.
        """
        if not self._gesture:
            self.stack.beginMacro("Edit blur")
        k = self.ensure_keyframe_at_playhead(track)
        self.stack.push(EditKeyframe(self, track.id, k.id, changes, merge_key))
        if not self._gesture:
            self.stack.endMacro()

    _gesture = False

    def begin_gesture(self, text: str) -> None:
        if not self._gesture:
            self._gesture = True
            self.stack.beginMacro(text)

    def end_gesture(self) -> None:
        if self._gesture:
            self._gesture = False
            self.stack.endMacro()

    def add_track(self) -> Track:
        tr = Track(name=self.project.next_track_name())
        tr.keyframes.append(self.new_keyframe_for(tr, self.time))
        self.stack.push(AddTrack(self, tr))
        return tr


# ==========================================================================
# commands
# ==========================================================================
class _Cmd(QUndoCommand):
    def __init__(self, doc: Document, text: str):
        super().__init__(text)
        self.doc = doc

    def _changed(self):
        self.doc.projectChanged.emit()


class AddTrack(_Cmd):
    def __init__(self, doc, track: Track, index: Optional[int] = None):
        super().__init__(doc, f"Add {track.name}")
        self.track = track
        self.index = len(doc.project.tracks) if index is None else index
        self.prev_sel = doc.selected_track_id

    def redo(self):
        self.doc.project.tracks.insert(self.index, self.track)
        self._changed()
        self.doc.select_track(self.track.id)

    def undo(self):
        self.doc.project.tracks.remove(self.track)
        self._changed()
        self.doc.select_track(self.prev_sel)


class DeleteTrack(_Cmd):
    def __init__(self, doc, track_id: str):
        tr = doc.project.track_by_id(track_id)
        super().__init__(doc, f"Delete {tr.name}")
        self.track = tr
        self.index = doc.project.track_index(track_id)

    def redo(self):
        self.doc.project.tracks.remove(self.track)
        self._changed()
        tracks = self.doc.project.tracks
        self.doc.select_track(tracks[min(self.index, len(tracks) - 1)].id if tracks else None)

    def undo(self):
        self.doc.project.tracks.insert(self.index, self.track)
        self._changed()
        self.doc.select_track(self.track.id)


class SetTrackProperty(_Cmd):
    """Rename / visible / sticky / lock_aspect."""

    LABELS = {"name": "Rename track", "visible": "Toggle visibility",
              "sticky": "Toggle sticky", "lock_aspect": "Toggle lock aspect"}

    def __init__(self, doc, track_id: str, attr: str, value):
        super().__init__(doc, self.LABELS.get(attr, f"Set {attr}"))
        self.track_id, self.attr, self.value = track_id, attr, value
        self.old = getattr(doc.project.track_by_id(track_id), attr)

    def _set(self, v):
        setattr(self.doc.project.track_by_id(self.track_id), self.attr, v)
        self._changed()

    def redo(self):
        self._set(self.value)

    def undo(self):
        self._set(self.old)


class ReorderTracks(_Cmd):
    def __init__(self, doc, new_order: list[str]):
        super().__init__(doc, "Reorder tracks")
        self.old_order = [t.id for t in doc.project.tracks]
        self.new_order = new_order

    def _apply(self, order):
        by_id = {t.id: t for t in self.doc.project.tracks}
        self.doc.project.tracks[:] = [by_id[i] for i in order]
        self._changed()

    def redo(self):
        self._apply(self.new_order)

    def undo(self):
        self._apply(self.old_order)


class AddKeyframe(_Cmd):
    def __init__(self, doc, track_id: str, kf: Keyframe):
        super().__init__(doc, "Add keyframe")
        self.track_id, self.kf = track_id, kf

    def redo(self):
        tr = self.doc.project.track_by_id(self.track_id)
        tr.keyframes.append(self.kf)
        tr.sort()
        self._changed()

    def undo(self):
        self.doc.project.track_by_id(self.track_id).keyframes.remove(self.kf)
        self._changed()


class DeleteKeyframe(_Cmd):
    def __init__(self, doc, track_id: str, kf_id: str):
        super().__init__(doc, "Delete keyframe")
        self.track_id = track_id
        self.kf = doc.project.track_by_id(track_id).keyframe_by_id(kf_id)

    def redo(self):
        self.doc.project.track_by_id(self.track_id).keyframes.remove(self.kf)
        self._changed()

    def undo(self):
        tr = self.doc.project.track_by_id(self.track_id)
        tr.keyframes.append(self.kf)
        tr.sort()
        self._changed()


class EditKeyframe(_Cmd):
    """Change keyframe properties (including time). Consecutive edits with the same
    merge_key on the same keyframe collapse into a single undo step."""

    ID = 1001

    def __init__(self, doc, track_id: str, kf_id: str, changes: dict, merge_key: Optional[str] = None):
        label = "Retime keyframe" if set(changes) == {"time"} else "Edit keyframe"
        super().__init__(doc, label)
        self.track_id, self.kf_id, self.merge_key = track_id, kf_id, merge_key
        kf = doc.project.track_by_id(track_id).keyframe_by_id(kf_id)
        self.new = dict(changes)
        self.old = {k: getattr(kf, k) for k in changes}

    def id(self):
        return self.ID if self.merge_key else -1

    def mergeWith(self, other):
        if (not isinstance(other, EditKeyframe) or other.merge_key != self.merge_key
                or other.kf_id != self.kf_id or other.track_id != self.track_id):
            return False
        for k, v in other.old.items():
            self.old.setdefault(k, v)
        self.new.update(other.new)
        return True

    def _apply(self, values):
        tr = self.doc.project.track_by_id(self.track_id)
        kf = tr.keyframe_by_id(self.kf_id)
        for k, v in values.items():
            setattr(kf, k, copy.copy(v))
        if "time" in values:
            tr.sort()
        self._changed()

    def redo(self):
        self._apply(self.new)

    def undo(self):
        self._apply(self.old)
