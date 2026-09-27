"""Preview audio: PCM decoding lines up with the video frames, and the player feeds its sink."""
import time

import numpy as np
import pytest

from keyblur import audio

from conftest import _ffmpeg

RATE = 48000


@pytest.fixture(scope="module")
def click_clips(tmp_path_factory):
    """A 4 s clip that is silent except for a tone from 2.0 s, as MP4 and as VOB."""
    d = tmp_path_factory.mktemp("audio")
    tone = "aevalsrc='if(gte(t,2),0.5*sin(2*PI*440*t),0)':s=48000:d=4"
    mp4, vob = str(d / "click.mp4"), str(d / "click.vob")
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=4", "-f", "lavfi", "-i", tone,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", mp4)
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=720x480:rate=30000/1001:duration=4", "-f", "lavfi", "-i", tone,
            "-c:v", "mpeg2video", "-c:a", "mp2", "-b:a", "192k", "-f", "vob", vob)
    return mp4, vob


def _onset(path, start):
    pcm = np.frombuffer(b"".join(audio.pcm_chunks(path, start, RATE)), np.int16).reshape(-1, 2)
    loud = np.nonzero(np.abs(pcm[:, 0]) > 2000)[0]
    return loud[0] / RATE, len(pcm) / RATE


@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("start", [0.0, 1.0, 1.5])
def test_pcm_starts_at_requested_time(click_clips, which, start):
    onset, length = _onset(click_clips[which], start)
    assert onset == pytest.approx(2.0 - start, abs=0.03)
    assert length == pytest.approx(4.0 - start, abs=0.1)


def test_no_audio_stream(media_dir):
    p = str(media_dir / "silent.mp4")
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=1", "-c:v", "libx264", p)
    assert list(audio.pcm_chunks(p, 0, RATE)) == []


class _FakeDev:
    def __init__(self):
        self.data = bytearray()

    def write(self, b):
        self.data += b
        return len(b)


class _FakeSink:
    def __init__(self):
        self.dev = _FakeDev()
        self.volume = None
        self.stopped = False

    def setBufferSize(self, n):
        pass

    def start(self):
        return self.dev

    def bytesFree(self):
        return 1 << 20

    def setVolume(self, v):
        self.volume = v

    def stop(self):
        self.stopped = True

    def deleteLater(self):
        pass


@pytest.mark.skipif(audio.QAudioSink is None, reason="Qt Multimedia unavailable")
def test_player_feeds_sink_in_step_with_clock(click_clips, monkeypatch):
    from PySide6.QtMultimedia import QAudioFormat
    fmt = QAudioFormat()
    fmt.setSampleRate(RATE)
    fmt.setChannelCount(2)
    fmt.setSampleFormat(QAudioFormat.Int16)
    sink = _FakeSink()
    monkeypatch.setattr(audio.AudioPlayer, "_make_sink", lambda self: (sink, fmt))
    p = audio.AudioPlayer()
    # the clock started 0.5 s ago: the first 0.5 s of audio is overdue and must be skipped
    assert p.start(click_clips[0], 1.0, time.perf_counter() - 0.5)
    deadline = time.time() + 5
    while time.time() < deadline and len(sink.dev.data) < RATE * 4 * 2:
        p.pump()
        time.sleep(0.01)
    p.set_muted(True)
    assert sink.volume == 0
    p.stop()
    assert sink.stopped and not p.active
    pcm = np.frombuffer(bytes(sink.dev.data), np.int16).reshape(-1, 2)
    onset = np.nonzero(np.abs(pcm[:, 0]) > 2000)[0][0] / RATE
    assert onset == pytest.approx(0.5, abs=0.05)  # tone at 2.0 s, started at 1.0 s, 0.5 s skipped
