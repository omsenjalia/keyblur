"""Final render: decode full-res frames, blur with blur.apply_blurs, pipe to ffmpeg."""
from __future__ import annotations

import copy
import os
import subprocess
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QWidget)

from . import video
from .blur import apply_blurs
from .model import Project

PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]


class ExportCancelled(Exception):
    pass


def output_geometry(project: Project, square_pixels: bool) -> tuple[Optional[str], str]:
    """Return (scale filter or None, setsar value) for the output video."""
    src = project.source
    if square_pixels and src.sar != 1:
        w = int(round(src.height * src.display_aspect / 2)) * 2
        return f"scale={w}:{src.height}:flags=lanczos", "1"
    return None, f"{src.sar.numerator}/{src.sar.denominator}"


def build_ffmpeg_cmd(project: Project, playable: str, out_path: str, settings: dict) -> list[str]:
    src = project.source
    fps = src.fps
    deint = settings.get("deinterlace")
    if deint is None:
        deint = src.interlaced
    scale, sar = output_geometry(project, bool(settings.get("square_pixels")))
    vf = []
    if deint:
        vf.append("yadif=mode=0")
    if scale:
        vf.append(scale)
    vf.append(f"setsar={sar}")
    codec = settings.get("codec", "libx264")
    cmd = [video.tool("ffmpeg"), "-hide_banner", "-y", "-nostdin", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{src.width}x{src.height}",
           "-framerate", f"{fps.numerator}/{fps.denominator}", "-i", "-",
           "-i", playable,
           "-map", "0:v:0", "-map", "1:a?",
           "-vf", ",".join(vf),
           "-c:v", codec, "-crf", str(int(settings.get("crf", 18))),
           "-preset", settings.get("preset", "medium"),
           "-pix_fmt", "yuv420p"]
    if codec == "libx265":
        cmd += ["-tag:v", "hvc1"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    if settings.get("container", "mp4") == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd.append(out_path)
    return cmd


def render(project: Project, playable: str, out_path: str, settings: dict,
           progress: Optional[Callable[[int, int], None]] = None,
           cancelled: Optional[Callable[[], bool]] = None) -> None:
    """Render the blurred video. Raises video.VideoError / ExportCancelled."""
    src = project.source
    total = src.frame_count
    cmd = build_ffmpeg_cmd(project, playable, out_path, settings)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, creationflags=video._NO_WINDOW)
    err: list[bytes] = []
    t = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    t.start()
    reader = video.FrameReader(playable, src.fps)
    ok = False
    try:
        n = 0
        for idx, frame in reader.iter_frames(0, total):
            if cancelled and cancelled():
                raise ExportCancelled()
            if frame.shape[1] != src.width or frame.shape[0] != src.height:
                raise video.VideoError(f"Decoded frame size {frame.shape[1]}x{frame.shape[0]} "
                                       f"does not match {src.width}x{src.height}")
            out = apply_blurs(frame, project.states_at(project.time_of(idx)), 1.0)
            try:
                proc.stdin.write(out.tobytes())
            except (BrokenPipeError, OSError):
                break
            n += 1
            if progress and (n % 5 == 0 or n == total):
                progress(n, total)
        ok = True
    finally:
        reader.close()
        try:
            proc.stdin.close()
        except OSError:
            pass
        if not ok:
            proc.kill()
        proc.wait()
        t.join(timeout=5)
        if not ok or proc.returncode != 0:
            _remove(out_path)
    if proc.returncode != 0:
        raise video.VideoError("ffmpeg failed:\n" + b"".join(err).decode(errors="replace")[-3000:])


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class ExportWorker(QThread):
    progress = Signal(int, int)
    done = Signal(bool, str)  # success, message

    def __init__(self, project: Project, playable: str, out_path: str, settings: dict, parent=None):
        super().__init__(parent)
        # snapshot so edits during export do not race with rendering
        self.project = copy.deepcopy(project)
        self.playable, self.out_path, self.settings = playable, out_path, dict(settings)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        t0 = time.time()
        try:
            render(self.project, self.playable, self.out_path, self.settings,
                   progress=self.progress.emit, cancelled=lambda: self._cancel)
        except ExportCancelled:
            self.done.emit(False, "Export cancelled.")
            return
        except Exception as e:  # noqa: BLE001 - reported to the user
            self.done.emit(False, str(e))
            return
        self.done.emit(True, f"Exported to {self.out_path} in {time.time() - t0:.1f} s")


class ExportDialog(QDialog):
    def __init__(self, project: Project, default_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Video")
        s = project.export_settings
        src = project.source
        self.path = QLineEdit(s.get("output_path") or default_path)
        browse = QPushButton("Browse…")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        path_w = QWidget()
        path_w.setLayout(row)

        self.container = QComboBox()
        self.container.addItems(["mp4", "mkv"])
        self.container.setCurrentText(s.get("container", "mp4"))
        self.codec = QComboBox()
        self.codec.addItems(["libx264", "libx265"])
        self.codec.setCurrentText(s.get("codec", "libx264"))
        self.crf = QSpinBox()
        self.crf.setRange(0, 51)
        self.crf.setValue(int(s.get("crf", 18)))
        self.crf.setToolTip("Lower = better quality / bigger file. 18 is visually lossless for x264.")
        self.preset = QComboBox()
        self.preset.addItems(PRESETS)
        self.preset.setCurrentText(s.get("preset", "medium"))
        self.deint = QCheckBox("Deinterlace (yadif)")
        d = s.get("deinterlace")
        self.deint.setChecked(src.interlaced if d is None else bool(d))
        if src.interlaced:
            self.deint.setToolTip("Source is interlaced (typical for DVD).")
        self.square = QCheckBox(f"Square pixels (resize to display aspect)")
        self.square.setChecked(bool(s.get("square_pixels")))
        self.square.setEnabled(src.sar != 1)

        form = QFormLayout(self)
        form.addRow("Output file", path_w)
        form.addRow("Container", self.container)
        form.addRow("Video codec", self.codec)
        form.addRow("Quality (CRF)", self.crf)
        form.addRow("Speed preset", self.preset)
        form.addRow("", self.deint)
        form.addRow("", self.square)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Export")
        form.addRow(bb)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        browse.clicked.connect(self._browse)
        self.container.currentTextChanged.connect(self._fix_ext)
        self.resize(560, self.sizeHint().height())

    def _fix_ext(self, ext: str):
        p = self.path.text().strip()
        if p:
            self.path.setText(os.path.splitext(p)[0] + "." + ext)

    def _browse(self):
        ext = self.container.currentText()
        p, _ = QFileDialog.getSaveFileName(self, "Export to", self.path.text(),
                                           f"Video (*.{ext});;All files (*)")
        if p:
            self.path.setText(p)

    def _accept(self):
        p = self.path.text().strip()
        if not p:
            return
        ext = "." + self.container.currentText()
        if not p.lower().endswith(ext):
            p = os.path.splitext(p)[0] + ext
            self.path.setText(p)
        self.accept()

    def settings(self) -> dict:
        return {
            "output_path": self.path.text().strip(),
            "codec": self.codec.currentText(),
            "crf": self.crf.value(),
            "preset": self.preset.currentText(),
            "container": self.container.currentText(),
            "deinterlace": self.deint.isChecked(),
            "square_pixels": self.square.isChecked(),
        }
