"""Preview audio: decode the source's audio with PyAV and play it through Qt Multimedia.

The video preview runs on a wall clock, so the audio is aligned to that same
clock when it starts (padded with silence or trimmed) and then simply streamed.
"""
from __future__ import annotations

import queue
import threading
import time
from fractions import Fraction
from typing import Iterator, Optional

import av

try:  # Qt Multimedia can be missing system libraries (e.g. PulseAudio on Linux)
    from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices
except ImportError:  # pragma: no cover - depends on the platform
    QAudioSink = None

_AV_FORMATS = {}
if QAudioSink is not None:
    _AV_FORMATS = {QAudioFormat.Int16: ("s16", 2), QAudioFormat.Int32: ("s32", 4),
                   QAudioFormat.Float: ("flt", 4), QAudioFormat.UInt8: ("u8", 1)}


def pcm_chunks(path: str, start: float, rate: int, channels: int = 2, fmt: str = "s16",
               stop: Optional[threading.Event] = None) -> Iterator[bytes]:
    """Yield interleaved PCM for the first audio stream, starting at ``start`` seconds.

    ``start`` is relative to the start of the *video* stream, i.e. frame index / fps,
    so the audio lines up with the frames the preview shows. Yields nothing if the
    file has no audio.
    """
    with av.open(path) as container:
        if not container.streams.audio:
            return
        astream = container.streams.audio[0]
        vstream = container.streams.video[0] if container.streams.video else None
        origin = 0.0
        if vstream is not None and vstream.start_time is not None:
            origin = float(vstream.start_time * vstream.time_base)
        elif astream.start_time is not None:
            origin = float(astream.start_time * astream.time_base)
        target = origin + max(0.0, start)
        seek_t = max(0.0, target - 0.5)
        container.seek(int(seek_t / astream.time_base), stream=astream, backward=True, any_frame=False)

        layout = "mono" if channels == 1 else "stereo" if channels == 2 else channels
        resampler = av.AudioResampler(format=fmt, layout=layout, rate=rate)
        frame_bytes = channels * av.AudioFormat(fmt).bytes
        pos: Optional[float] = None  # source time of the next output sample

        def emit(out_frames) -> Iterator[bytes]:
            nonlocal pos
            for of in out_frames:
                data = bytes(of.planes[0])[:of.samples * frame_bytes]
                if pos is None:
                    pos = target
                end = pos + of.samples / rate
                if end <= target:
                    pos = end
                    continue
                if pos < target:
                    data = data[int(round((target - pos) * rate)) * frame_bytes:]
                    pos = target
                pos = end
                if data:
                    yield data

        try:
            packets = container.demux(astream)
            for packet in packets:
                if stop is not None and stop.is_set():
                    return
                try:
                    frames = packet.decode()
                except av.FFmpegError:
                    continue  # corrupt packet
                for f in frames:
                    if pos is None and f.pts is not None and f.time_base is not None:
                        first = float(f.pts * f.time_base)
                        if first + f.samples / f.sample_rate <= target:
                            continue  # still before the start point
                        if first > target:  # gap before the first sample: pad with silence
                            gap = int(round((first - target) * rate))
                            pos = target
                            if gap > 0:
                                yield bytes(gap * frame_bytes)
                                pos = first
                        else:
                            pos = first
                    yield from emit(resampler.resample(f))
            yield from emit(resampler.resample(None))
        except av.EOFError:
            pass


class AudioPlayer:
    """Plays a file's audio from a given time. All methods must be called from the GUI thread.

    Call :meth:`pump` regularly (every few ms) while playing to feed the output device.
    """

    def __init__(self):
        self.volume = 1.0
        self.muted = False
        self._sink = None
        self._dev = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._buf = b""
        self._clock0 = 0.0
        self._aligned = False
        self._bpf = 4
        self._rate = 48000
        self.error: Optional[str] = None

    @property
    def active(self) -> bool:
        return self._sink is not None

    def _make_sink(self):
        if QAudioSink is None:
            return None, None
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            return None, None
        fmt = device.preferredFormat()
        rate = fmt.sampleRate() if fmt.sampleRate() > 0 else 48000
        want = QAudioFormat()
        want.setSampleRate(rate)
        want.setChannelCount(2)
        want.setSampleFormat(QAudioFormat.Int16)
        if device.isFormatSupported(want):
            fmt = want
        elif fmt.sampleFormat() not in _AV_FORMATS or fmt.channelCount() < 1:
            return None, None
        return QAudioSink(device, fmt), fmt

    def start(self, path: str, start_sec: float, clock0: float) -> bool:
        """Start playing ``path`` from ``start_sec`` so that ``start_sec`` sounds at
        ``time.perf_counter() == clock0``. Returns False if no audio output is available."""
        self.stop()
        self.error = None
        try:
            sink, fmt = self._make_sink()
        except Exception as e:  # noqa: BLE001 - audio is optional
            self.error = str(e)
            return False
        if sink is None:
            return False
        av_fmt, sample_bytes = _AV_FORMATS[fmt.sampleFormat()]
        channels = fmt.channelCount()
        self._rate = fmt.sampleRate()
        self._bpf = sample_bytes * channels
        sink.setBufferSize(int(self._rate * 0.1) * self._bpf)  # ~100 ms: low latency, no underruns
        self._sink = sink
        self._apply_volume()
        self._dev = sink.start()
        self._buf = b""
        self._clock0 = clock0
        self._aligned = False
        self._stop = threading.Event()
        self._q = queue.Queue(maxsize=64)
        q, stop = self._q, self._stop

        def run():
            try:
                for chunk in pcm_chunks(path, start_sec, self._rate, channels, av_fmt, stop):
                    while not stop.is_set():
                        try:
                            q.put(chunk, timeout=0.1)
                            break
                        except queue.Full:
                            continue
                    if stop.is_set():
                        return
            except Exception as e:  # noqa: BLE001 - playback continues without sound
                self.error = str(e)

        self._thread = threading.Thread(target=run, name="keyblur-audio", daemon=True)
        self._thread.start()
        return True

    def pump(self) -> None:
        sink = self._sink
        if sink is None or self._dev is None:
            return
        free = sink.bytesFree()
        while free > 0:
            if not self._buf:
                try:
                    self._buf = self._q.get_nowait()
                except queue.Empty:
                    return
                if not self._aligned:
                    self._align()
                    continue
            n = self._dev.write(self._buf[:free])
            if n <= 0:
                return
            self._buf = self._buf[n:]
            free -= n

    def _align(self) -> None:
        """Pad or trim the first chunk so it sounds in step with the video clock."""
        self._aligned = True
        late = time.perf_counter() - self._clock0
        n = int(round(abs(late) * self._rate)) * self._bpf
        if late < 0:
            self._buf = bytes(n) + self._buf
        else:
            # drop what is already overdue, pulling more chunks if needed
            while n > 0:
                if len(self._buf) > n:
                    self._buf = self._buf[n:]
                    return
                n -= len(self._buf)
                try:
                    self._buf = self._q.get_nowait()
                except queue.Empty:
                    self._buf = b""
                    return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            try:
                while True:
                    self._q.get_nowait()
            except queue.Empty:
                pass
            self._thread.join(2)
            self._thread = None
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
        self._sink = None
        self._dev = None
        self._buf = b""

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(1.0, volume))
        self._apply_volume()

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        self._apply_volume()

    def _apply_volume(self) -> None:
        if self._sink is not None:
            v = 0.0 if self.muted else QAudio.convertVolume(self.volume, QAudio.LogarithmicVolumeScale,
                                                             QAudio.LinearVolumeScale)
            self._sink.setVolume(v)
