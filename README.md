# KeyBlur

A keyframe-based video blur tool for Windows. Load a video (including DVD `.vob` files and `VIDEO_TS` folders), mark the moments to blur, place circle or oval blur regions (static or moving), save the session as a `.keyblur` project, and export the blurred video.

The full walkthrough is in **[GUIDE.md](GUIDE.md)** (press F1 in the app).

## Features

- **In/Out range workflow:** press I and O, then Ctrl+B to blur that span. Press Ctrl+B again for simultaneous blurs.
- **Gaps on one track:** press X or use Remove blur In→Out. For example: blur at 1–2 s, nothing at 2–3 s, two blurs at 3–5 s.
- **Keyframe motion:** Hold (jump), Linear, Ease, or Off (no blur until the next key).
- **Precision timeline:**
  - millisecond timecode and per-frame ticks
  - hover read-out
  - snapping to seconds, keys, playhead and In/Out
  - a Go-to-time box and an editable keyframe time
  - zoom and a scrollbar
- **Canvas editing:** drag, resize and Alt+wheel for strength. Editing when the playhead isn't on a keyframe adds one.
- **Live preview:** uses the same blur code as the export, and plays the source audio (mute with M).
- **Working with files:**
  - undo/redo
  - autosave and crash recovery
  - drag & drop
  - relinking of missing source files
- **Export:** full resolution through ffmpeg (x264/x265), with the source audio, optional deinterlacing and square-pixel output.

## Run

Double-click `run_keyblur.bat`, or run:

```
.venv\Scripts\python -m keyblur [project.keyblur | video file | VIDEO_TS folder]
```

You need ffmpeg and ffprobe on PATH, or set their location with **File > Set ffmpeg Location…**.

### Fresh setup

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Project file

A `.keyblur` file is JSON:

- The source path is saved relative to the project when possible. If the file is missing when you open the project, you're asked to locate it.
- Positions and radii are stored as fractions of the frame (0–1). Strength is the Gaussian sigma in source pixels.
- Each keyframe's `interpolation` is one of `hold`, `linear`, `ease` or `off`.

## Layout

| Path | Purpose |
|---|---|
| `keyblur/model.py` | data model, interpolation, time parsing, JSON I/O (no Qt) |
| `keyblur/video.py` | probing, VOB/VIDEO_TS remux, frame-accurate reader |
| `keyblur/audio.py` | preview audio playback (PyAV decode → Qt Multimedia) |
| `keyblur/blur.py` | elliptical blur shared by the preview and export |
| `keyblur/commands.py` | document, undo commands, range / cut / duplicate operations |
| `keyblur/export.py` | export dialog and ffmpeg render worker |
| `keyblur/widgets/` | canvas, timeline, track list, inspector |

## Tests

```
.venv\Scripts\python -m pytest -q
```

The tests generate synthetic VOB, VIDEO_TS and MP4 media with ffmpeg. They check:
- interpolation and gaps
- the range workflow
- JSON round-trips
- frame-accurate seeking
- VIDEO_TS concatenation
- export output
- the GUI interactions, run offscreen
