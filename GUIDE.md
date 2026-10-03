# KeyBlur User Guide

KeyBlur does one job: it blurs parts of a video at exactly the moments you choose, and exports the result. This guide goes from opening a video to exporting a finished file. **F1** in the app opens this guide.

---

## 1. The screen

```
┌───────────────────────────────────────────┬──────────────────────┐
│                                           │ Tracks               │
│   Video preview                           │  one row per blur    │
│   - drag a circle to move it              │                      │
│   - drag the square handles to resize it  │ Keyframe Inspector   │
│                                           │  exact numbers       │
├───────────────────────────────────────────┴──────────────────────┤
│ − + Fit  Zoom In→Out  Snap  │ [Set In  Set Out]  Clear   In/Out  │
│ 0:00.0     0:00.5     0:01.0     0:01.5     ← time ruler         │
│ Track 1         ◆━━━━━━━━]              ← coloured bar = blur on │
│ Track 2                        ◆━━━━━━━━━━━━]                    │
│ ⏮ ⏪ ▶ ⏩ ⏭  ──────●──────── 00:01.234 / 01:20.000   Go to: [   ] │
│ [Blur In→Out (new track)] [Add In→Out to track] [Remove blur …]  │
└──────────────────────────────────────────────────────────────────┘
```

- **Track:** one blur circle. Add more tracks to blur several things at the same moment.
- **Coloured bar:** when that track is blurring. No bar means no blur.
- **Keyframe:** a marker on the bar. It stores the circle's position, size and strength at that moment.
- **Red line:** the playhead, meaning the frame shown in the preview.
- **In/Out range:** a blue shaded area on the timeline. It's the easiest way to say "blur from here to there".

---

## 2. Open a video

**File > Open Video** (Ctrl+Shift+O), or drag a file onto the window. You can open:

- normal video files: `.mp4`, `.mkv`, `.mov`, `.avi`, …
- a single DVD `.vob` file
- a whole DVD: **File > Import VIDEO_TS Folder** (or drag the folder in)

DVD video is converted once into a smooth-seeking copy, which takes a few seconds. Nothing is re-encoded and your original is never modified.

---

## 3. The basic recipe: blur from one time to another

Example: blur a face from **1 s** to **2 s**.

1. Move the playhead to **1 s**. Click the ruler, or type `1` in **Go to** and press Enter.
2. Press **I** to set the **In** point.
3. Move to **2 s** and press **O** to set the **Out** point. The range is shaded blue.
4. Press **Ctrl+B**, or click **Blur In→Out (new track)**. A blur circle appears.
5. Drag the circle onto the face. Drag a corner handle to resize it.

That's it. The blur is on from 1 s to 2 s and off everywhere else.

> Instead of pressing I and O, you can **Shift+drag across the timeline** to select a range.

---

## 4. Your workflow: blur, then nothing, then several blurs

Goal:

| Time | What you want |
|---|---|
| 1 s – 2 s | one blur |
| 2 s – 3 s | no blur |
| 3 s – 5 s | two things blurred at the same time |

Steps:

1. **One blur, 1–2 s:** set In at 1 s and Out at 2 s, then press **Ctrl+B**. Position the circle.
2. **No blur, 2–3 s:** nothing to do. No bar means no blur.
3. **Two blurs, 3–5 s:**
   1. Set In at 3 s and Out at 5 s.
   2. Press **Ctrl+B** for the first circle.
   3. Press **Ctrl+B** again for the second. It appears slightly offset so the two don't overlap.
   4. Drag each circle onto its target. Click a circle, or its row in the timeline, to select it.
4. Press **Space** to play it back and check.

Each extra Ctrl+B adds another simultaneous blur in the current range.

### Reusing the same blur later (same track, second time range)

To blur the *same* thing again at, say, 8–9 s without another track:

1. Select the track.
2. Set In at 8 s and Out at 9 s.
3. Click **Add In→Out to track** (Ctrl+Shift+B).

The track then has two bars with a gap between them.

### Removing blur from part of a track

1. Select the track.
2. Set In/Out around the part that should **not** be blurred.
3. Click **Remove blur In→Out** (Shift+Del).

The bar is split and the blur is off in that part.

### Turning a blur off or on at the playhead

Press **X** (the **Blur OFF/ON from here** button).
- **If the blur is on,** it turns off from this frame until the next keyframe.
- **If it's off,** it turns back on from this frame.

A typical way to work while watching:
1. **Ctrl+T** starts a blur at the playhead that runs to the end.
2. Play the video.
3. Pause where the blur should stop and press **X**.

---

## 5. Making a blur follow something that moves

A blur holds still unless you tell it to move. To follow a moving object:

1. Go to the start of the movement and place the circle.
2. Move a few frames or seconds later (arrow keys, **Shift+arrow** jumps 1 s).
3. Drag the circle to the new position. A **keyframe is added automatically.**
4. Repeat as needed.

Between keyframes, the circle moves according to the **After key** setting of the earlier keyframe:

| After key | Timeline marker | What it does |
|---|---|---|
| **Linear** | ◆ diamond | glides at constant speed to the next keyframe |
| **Ease** | ● circle | glides, starting and stopping gently |
| **Hold** | ■ square | stays still, then **jumps** to the next keyframe's position |
| **Off** | ] bracket | **no blur** until the next keyframe |

To change it, select the keyframe (click its marker) and use **After key** in the inspector. You can also right-click the marker, or press **Alt+1 / 2 / 3 / 4**. The **New keys** dropdown sets the mode for newly created keyframes (**Hold** by default). To convert existing keyframes in one go, use **Edit → Set All Keys on Track** or **Set All Keys in Project** (Off keys are left alone, and one Ctrl+Z undoes it).

**Changing several keyframes at once:**

1. Hold **Ctrl** and drag a box over the timeline. Every keyframe inside it gets a yellow outline. You can box across several tracks.
2. **Ctrl+click** a keyframe to add it to the selection or take it out. **Ctrl+A** selects every keyframe.
3. Press **Alt+1 / 2 / 3 / 4**, or right-click one of the selected keyframes, to set them all to Hold / Linear / Ease / Off. **Del** deletes them.
4. Click an empty part of the timeline, or press **Esc**, to clear the selection.

To give different parts different types, select one group, set it, then select the next group. Each change is a single Ctrl+Z.

> Tip: for a blur that jumps between spots (1 s here, 2 s there, 3 s somewhere else), use **Hold**.

---

## 6. Precision: hitting exact times

- **Go to:** type a time and press Enter (focus it with Ctrl+G). It accepts:
  - `3` (seconds)
  - `3.25` (seconds with a fraction)
  - `1:02.5` (minutes:seconds)
  - `0:01:02` (hours:minutes:seconds)
  - `f120` (frame 120)
- **Frame stepping:** the ← / → keys or the ⏪ ⏩ buttons move one frame. Shift+← / → moves one second.
- **Keyframe time:** with the playhead on a keyframe, type an exact **Key time** in the inspector.
- **Zoom:** use Ctrl+wheel over the timeline, the **+ / −** buttons, or the **= / -** keys. Zoomed in far enough, each frame gets its own tick.
- **Zoom In→Out:** fills the timeline with the selected range.
- **Hover read-out:** point at the timeline to see the exact time and frame number under the mouse.
- **Snap:** when on, dragging snaps to:
  - whole seconds (and tenths when zoomed in)
  - other keyframes
  - the playhead
  - In/Out

  A yellow line shows the snap. Hold **Shift** while dragging a keyframe to switch snapping off (or on) for that drag.
- **Timecode:** times are shown as `minutes:seconds.milliseconds`. DVD/NTSC video runs at 29.97 frames per second, so an exact whole second usually falls between two frames. KeyBlur picks the nearest frame, so 1 s shows as `00:01.001`. That's normal.

---

## 7. Adjusting the blur

- **Size:** drag a corner or edge handle, or use Radius X / Radius Y in the inspector.
- **Circle or oval:** **Lock aspect** keeps it a perfect circle on screen (on by default). Untick it to stretch it into an oval.
- **Strength:** the inspector slider, or **Alt+mouse wheel** over the video.
- **Preview blur (B):** turns the blur off in the preview so you can see what's underneath. Export is not affected.
- **Outlines (H):** hides the circles and handles so you see a clean preview.

---

## 8. Tracks

- **Visible checkbox:** a hidden track is not blurred in the preview **or the export**.
- **To end checkbox:** the blur stays on after its last keyframe until the video ends.
- **Rename:** double-click a name.
- **Reorder:** drag a track, or use ▲ ▼.
- **Duplicate (Ctrl+D):** a quick way to make a second blur with the same timing.
- **Delete:** Ctrl+Shift+Del, or the Delete button.

---

## 9. Timeline mouse actions

| Action | Result |
|---|---|
| Click the ruler or empty space | move the playhead |
| Click a keyframe marker | select it and jump to it |
| Drag a keyframe marker | move it in time |
| Double-click a track row | add a keyframe there |
| Shift+drag | select an In/Out range |
| Ctrl+drag / Ctrl+click | select several keyframes |
| Drag the blue In/Out edge on the ruler | adjust the range |
| Right-click | menu: add or delete keyframes, blur on/off, range actions |
| Mouse wheel / Ctrl+wheel | scroll / zoom |

---

## 10. Save, resume, export

- **Save (Ctrl+S)** writes a small `.keyblur` project file. Open it later to continue exactly where you stopped.
- **Moved video:** if the video was moved, KeyBlur asks you to locate it.
- **Autosave:** KeyBlur autosaves every minute. After a crash it offers to restore your work.
- **Export (Ctrl+E):**
  1. Choose a file name and quality. CRF 18 is visually lossless; lower means better quality and larger files.
  2. For DVD sources, keep **Deinterlace** on. **Square pixels** turns 720×480 DVD video into a normal 640×480 (4:3) or 854×480 (16:9) picture.
  3. The export is full resolution, keeps the original audio, and uses exactly the blur you saw in the preview.

---

## 11. All shortcuts

| Key | Action |
|---|---|
| Space | Play / pause |
| ← / → | Previous / next frame |
| Shift+← / → | Back / forward 1 second |
| [ / ] | Previous / next keyframe |
| Home / End | Start / end of video |
| Ctrl+G | Go to time |
| M | Mute / unmute preview audio |
| **I / O** | **Set In / Out** |
| Shift+I / Shift+O | Jump to In / Out |
| Alt+X | Clear In/Out |
| **Ctrl+B** | **Blur In→Out as a new track** |
| Ctrl+Shift+B | Blur In→Out on the selected track |
| Shift+Del | Remove blur In→Out from the selected track |
| **X** | **Blur off / on from the playhead** |
| K | Add keyframe |
| Del | Delete keyframe at the playhead (or all selected keyframes) |
| Ctrl+A | Select all keyframes |
| Alt+1 / 2 / 3 / 4 | After key: Hold / Linear / Ease / Off (applies to all selected keyframes) |
| Ctrl+T | New track (uses the In/Out range if set) |
| Ctrl+D | Duplicate track |
| B / H | Preview blur / outlines on/off |
| = / - / Ctrl+0 | Timeline zoom in / out / fit |
| Ctrl+Z / Ctrl+Y | Undo / redo |
| Ctrl+S / Ctrl+E | Save / export |
| F1 | This guide |

---

## 12. Troubleshooting

| Problem | Fix |
|---|---|
| "ffmpeg not found" | Install ffmpeg (`winget install Gyan.FFmpeg`) or use **File > Set ffmpeg Location**. |
| The DVD shows the wrong title | Use **Import VIDEO_TS Folder** and choose another title set when asked. |
| No blur in the export | Check the track's **Visible** box, and that its bar covers that time. |
| The circle looks stretched | Turn on **Lock aspect**. |
| No sound when playing | Check the speaker button and volume slider next to **Go to**, and your Windows default playback device. Sound plays only during playback, not while scrubbing. |
| Disk space | DVD copies are cached in `%LOCALAPPDATA%\KeyBlur\cache` and can be deleted at any time. |
