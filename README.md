# KeyBlur

A keyframe-based video blur tool. Load a video (including DVD `.vob` files and `VIDEO_TS` folders), draw circle or ellipse blur regions that move over time, save the session as a `.keyblur` project, and export a blurred video.

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

## Workflow

1. **File > Open Video…** or **Import VIDEO_TS Folder…**. VOB sources are remuxed once, losslessly, into `%LOCALAPPDATA%\KeyBlur\cache` so seeking is frame-accurate.
2. **+ Add Track** (Ctrl+T) creates a blur track with a keyframe at the playhead.
3. Drag the circle to move it, and drag its handles to resize it. If the playhead isn't on a keyframe, editing adds one there (auto-key). Each drag is one undo step.
4. Each keyframe's **Interpolation** sets how it moves to the next one:
   - **Hold** — hard cut at the next keyframe
   - **Linear** — constant-speed movement
   - **Ease** — smooth start and stop
5. **Sticky** keeps a track's blur on from its last keyframe to the end of the video. Otherwise a track is active from its first keyframe to its last.
6. **Lock aspect** keeps the region a true circle on screen, even on anamorphic DVD video.
7. **File > Export Video…** (Ctrl+E) lets you choose codec, CRF, preset, deinterlacing and square pixels. The export uses the same blur code as the preview, at full resolution, and keeps the source audio.

## Timeline

- Click to seek.
- Drag a diamond to retime it. It snaps to frames.
- Right-click to add or delete keyframes and set interpolation.
- Ctrl+wheel zooms and the wheel scrolls.

Diamond shapes show the interpolation mode:

| Shape | Mode |
|---|---|
| ◆ | linear |
| ■ | hold |
| rounded | ease |

## Shortcuts

| Key | Action |
|---|---|
| Space | Play / pause |
| ← / → | Previous / next frame |
| Shift+← / → | Back / forward 1 second |
| [ / ] | Previous / next keyframe |
| K / Del | Add / delete keyframe at the playhead |
| Alt+1/2/3 | Hold / Linear / Ease |
| Alt+wheel on canvas | Blur strength |
| B / O | Toggle preview blur / outlines |
| Ctrl+Z / Ctrl+Y | Undo / redo |
| Ctrl+S / Ctrl+E | Save / export |

## Project file

A `.keyblur` file is JSON:
- The source path is saved relative to the project when possible. If the file is missing when you open the project, you're asked to locate it.
- Positions and radii are stored as fractions of the frame (0–1). Strength is the Gaussian sigma in source pixels.

## Tests

```
.venv\Scripts\python -m pytest -q
```

The tests generate synthetic VOB, VIDEO_TS and MP4 media with ffmpeg. They check:
- interpolation
- JSON round-trips
- frame-accurate seeking
- VIDEO_TS concatenation
- export output
- the GUI interactions, run offscreen
