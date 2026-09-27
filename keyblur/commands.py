"""Document (project + selection + undo stack) and undoable edit commands."""
from __future__ import annotations

import copy
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoCommand, QUndoStack

from .model import BlurState, Keyframe, Project, Track, fmt_time

DEFAULT_RADIUS = 0.08
DEFAULT_STRENGTH = 25.0


class Document(QObject):
    """Holds the project and UI selection; widgets observe its signals."""

    projectChanged = Signal()      # any track/keyframe data changed
    selectionChanged = Signal()    # selected track changed
    playheadChanged = Signal(int)  # current frame index
    sourceChanged = Signal()       # a new video was loaded / project replaced
    rangeChanged = Signal()        # In/Out range changed
    message = Signal(str)          # short user-facing status message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = Project()
        self.path: Optional[str] = None
        self.playable_path: Optional[str] = None
        self.stack = QUndoStack(self)
        self.selected_track_id: Optional[str] = None
        self.frame = 0
        self.default_interpolation = "linear"
        self.in_frame: Optional[int] = None
        self.out_frame: Optional[int] = None

    # ---- state -----------------------------------------------------------
    def reset(self, project: Project, path: Optional[str], playable: Optional[str]) -> None:
        self.project = project
        self.path = path
        self.playable_path = playable
        self.stack.clear()
        self.stack.setClean()
        self.selected_track_id = project.tracks[0].id if project.tracks else None
        self.frame = 0
        self.in_frame = self.out_frame = None
        self.sourceChanged.emit()
        self.projectChanged.emit()
        self.selectionChanged.emit()
        self.rangeChanged.emit()
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
        return track.governing_keyframe(self.time) or track.keyframes[0]

    def display_state(self, track: Track, t: Optional[float] = None) -> BlurState:
        """State to show/edit for a track, even where it is not blurring."""
        t = self.time if t is None else t
        s = track.state_at(t)
        if s is not None:
            return s
        if track.keyframes:
            k = track.governing_keyframe(t) or track.keyframes[0]
            return BlurState(k.x, k.y, k.radius_x, k.radius_y, k.strength, track.id)
        ry = DEFAULT_RADIUS * self.aspect
        return BlurState(0.5, 0.5, DEFAULT_RADIUS, ry, DEFAULT_STRENGTH, track.id)

    @property
    def aspect(self) -> float:
        """Display aspect: radius_y = radius_x * aspect gives a visual circle."""
        return self.project.source.display_aspect if self.project.source else 16 / 9

    # ---- In/Out range ---------------------------------------------------------
    def set_in(self, frame: Optional[int] = None) -> None:
        self.in_frame = self.frame if frame is None else frame
        if self.out_frame is not None and self.out_frame <= self.in_frame:
            self.out_frame = None
        self.rangeChanged.emit()

    def set_out(self, frame: Optional[int] = None) -> None:
        self.out_frame = self.frame if frame is None else frame
        if self.in_frame is not None and self.in_frame >= self.out_frame:
            self.in_frame = None
        self.rangeChanged.emit()

    def set_range(self, a: int, b: int) -> None:
        a, b = sorted((a, b))
        if b > a:
            self.in_frame, self.out_frame = a, b
            self.rangeChanged.emit()

    def clear_range(self) -> None:
        self.in_frame = self.out_frame = None
        self.rangeChanged.emit()

    @property
    def has_range(self) -> bool:
        return self.in_frame is not None and self.out_frame is not None

    def range_times(self) -> Optional[tuple[float, float]]:
        if not self.has_range:
            return None
        return self.project.time_of(self.in_frame), self.project.time_of(self.out_frame)

    # ---- high-level edit helpers ------------------------------------------
    def new_keyframe_for(self, track: Track, t: float) -> Keyframe:
        s = self.display_state(track, t)
        prev = track.governing_keyframe(t - 1e-3)
        interp = prev.interpolation if prev and prev.interpolation != "off" else self.default_interpolation
        return Keyframe(time=t, x=s.x, y=s.y, radius_x=s.radius_x, radius_y=s.radius_y,
                        strength=s.strength, interpolation=interp)

    def ensure_keyframe_at_playhead(self, track: Track) -> Keyframe:
        """Return the keyframe at the playhead, creating one (auto-key) if needed.

        Auto-keying inside an "off" gap turns the blur back on from here."""
        k = track.keyframe_at(self.time)
        if k is None:
            k = self.new_keyframe_for(track, self.time)
            self.stack.push(AddKeyframe(self, track.id, k))
        elif k.interpolation == "off":
            self.stack.push(EditKeyframe(self, track.id, k.id,
                                         {"interpolation": self.default_interpolation}))
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

    def _cascade_state(self, t0: float, t1: float) -> BlurState:
        """Default position for a new blur, offset from blurs already active in [t0, t1)."""
        n = 0
        for tr in self.project.tracks:
            if any(a < t1 and b >= t0 for a, b in tr.active_segments(self.project.duration)):
                n += 1
        offsets = [(0, 0), (0.15, 0), (-0.15, 0), (0, 0.2), (0, -0.2), (0.15, 0.2), (-0.15, 0.2)]
        dx, dy = offsets[n % len(offsets)]
        return BlurState(0.5 + dx, 0.5 + dy, DEFAULT_RADIUS, DEFAULT_RADIUS * self.aspect,
                         DEFAULT_STRENGTH)

    def add_track(self) -> Optional[Track]:
        """New track. With an In/Out range it blurs just that range; otherwise it
        starts at the playhead and runs to the end ("sticky") until you cut it."""
        if self.has_range:
            return self.blur_range(new_track=True)
        tr = Track(name=self.project.next_track_name(), sticky=True)
        s = self._cascade_state(self.time, self.project.duration)
        tr.keyframes.append(Keyframe(self.time, s.x, s.y, s.radius_x, s.radius_y, s.strength,
                                     self.default_interpolation))
        self.stack.push(AddTrack(self, tr))
        return tr

    def blur_range(self, new_track: bool = True) -> Optional[Track]:
        """Blur exactly the In/Out range: on a new track, or on the selected one."""
        rt = self.range_times()
        if rt is None:
            self.message.emit("Set an In point (I) and an Out point (O) first.")
            return None
        t0, t1 = rt
        tr = None if new_track else self.selected_track
        if tr is None:
            tr = Track(name=self.project.next_track_name())
            s = self._cascade_state(t0, t1)
            tr.keyframes = [
                Keyframe(t0, s.x, s.y, s.radius_x, s.radius_y, s.strength, self.default_interpolation),
                Keyframe(t1, s.x, s.y, s.radius_x, s.radius_y, s.strength, "off"),
            ]
            self.stack.push(AddTrack(self, tr))
        else:
            self.stack.beginMacro(f"Blur range on {tr.name}")
            after = tr.state_at(t1)
            after_key = tr.governing_keyframe(t1)
            s = self.display_state(tr, t0)
            for k in [k for k in tr.keyframes if t0 - 1e-4 <= k.time < t1 - 1e-4]:
                self.stack.push(DeleteKeyframe(self, tr.id, k.id))
            self.stack.push(AddKeyframe(self, tr.id, Keyframe(
                t0, s.x, s.y, s.radius_x, s.radius_y, s.strength, self.default_interpolation)))
            self._close_range_end(tr, t1, after, after_key)
            self.stack.endMacro()
        self.select_track(tr.id)
        self.set_frame(self.in_frame)
        return tr

    def remove_blur_range(self) -> None:
        """Turn the selected track's blur off inside the In/Out range."""
        rt = self.range_times()
        tr = self.selected_track
        if rt is None or tr is None:
            self.message.emit("Select a track and set an In/Out range first.")
            return
        t0, t1 = rt
        self.stack.beginMacro(f"Remove blur from {tr.name}")
        after = tr.state_at(t1)
        after_key = tr.governing_keyframe(t1)
        s = self.display_state(tr, t0)
        for k in [k for k in tr.keyframes if t0 - 1e-4 <= k.time < t1 - 1e-4]:
            self.stack.push(DeleteKeyframe(self, tr.id, k.id))
        if tr.governing_keyframe(t0) is not None or after is not None:
            self.stack.push(AddKeyframe(self, tr.id, Keyframe(
                t0, s.x, s.y, s.radius_x, s.radius_y, s.strength, "off")))
        if after is not None and tr.keyframe_at(t1) is None:
            interp = after_key.interpolation if after_key and after_key.interpolation != "off" \
                else self.default_interpolation
            self.stack.push(AddKeyframe(self, tr.id, Keyframe(
                t1, after.x, after.y, after.radius_x, after.radius_y, after.strength, interp)))
        self.stack.endMacro()

    def _close_range_end(self, tr: Track, t1: float, after, after_key) -> None:
        """After writing a blur segment ending at t1, restore what followed it."""
        if tr.keyframe_at(t1) is not None:
            return
        if after is not None:  # it was blurring at t1 before: resume that
            interp = after_key.interpolation if after_key and after_key.interpolation != "off" \
                else self.default_interpolation
            k = Keyframe(t1, after.x, after.y, after.radius_x, after.radius_y, after.strength, interp)
        else:
            s = self.display_state(tr, t1 - 1e-3)
            k = Keyframe(t1, s.x, s.y, s.radius_x, s.radius_y, s.strength, "off")
        self.stack.push(AddKeyframe(self, tr.id, k))

    def toggle_blur_at_playhead(self, track: Optional[Track] = None) -> None:
        """Cut the blur off from the playhead, or turn it back on if it is off here."""
        tr = track or self.selected_track
        if tr is None:
            return
        t = self.time
        k = tr.keyframe_at(t)
        if tr.state_at(t) is not None:
            if k is not None:
                self.stack.push(EditKeyframe(self, tr.id, k.id, {"interpolation": "off"}))
            else:
                s = self.display_state(tr)
                self.stack.push(AddKeyframe(self, tr.id, Keyframe(
                    t, s.x, s.y, s.radius_x, s.radius_y, s.strength, "off")))
            self.message.emit(f"{tr.name}: blur OFF from {fmt_time(t)}")
        else:
            if k is not None:
                self.stack.push(EditKeyframe(self, tr.id, k.id,
                                             {"interpolation": self.default_interpolation}))
            else:
                self.stack.push(AddKeyframe(self, tr.id, self.new_keyframe_for(tr, t)))
            self.message.emit(f"{tr.name}: blur ON from {fmt_time(t)}")

    def duplicate_track(self) -> Optional[Track]:
        tr = self.selected_track
        if tr is None:
            return None
        dup = Track(name=f"{tr.name} copy", visible=True, sticky=tr.sticky, lock_aspect=tr.lock_aspect,
                    keyframes=[Keyframe(k.time, min(1.0, k.x + 0.06), k.y, k.radius_x, k.radius_y,
                                        k.strength, k.interpolation) for k in tr.keyframes])
        self.stack.push(AddTrack(self, dup, self.project.track_index(tr.id) + 1))
        return dup

    def retime_keyframe(self, track: Track, kf: Keyframe, t: float,
                        merge_key: Optional[str] = None) -> bool:
        """Move a keyframe to time t (snapped), keeping it between its neighbours."""
        idx = track.keyframes.index(kf)
        one = self.project.time_of(1)
        lo = track.keyframes[idx - 1].time + one if idx > 0 else 0.0
        hi = track.keyframes[idx + 1].time - one if idx + 1 < len(track.keyframes) else self.project.duration
        t = self.project.snap(min(max(t, lo), hi))
        if abs(t - kf.time) < 1e-6:
            return False
        self.stack.push(EditKeyframe(self, track.id, kf.id, {"time": t}, merge_key))
        return True


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
