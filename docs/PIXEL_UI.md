# Pixel scenes and offline previews

The display scenes use the native 72×16 pixel grid. Font layout, motion and
colors are shared by on-device `.anim` assets and offline previews, so reviewing
an image does not require an attached BUSY Bar or an active Codex session.

## Components

- `pixel_ui.py`: proportional bitmap font layout, clipped BGRA canvas,
  frame-based slide/fade transitions, and a lossless PNG encoder.
- `pixel_fonts.py`: the project's 12-pixel effort font. It deliberately contains
  only the uppercase letters used by the effort labels; unsupported characters
  produce an explicit error. Glyphs have two-pixel stems and open counters.
- `effort_animation.py`: effort-specific palettes and motion. `frame()` renders
  one frame; `frames()` generates the sequence used by `install_effort_anims.py`.
- `preview_effort.py`: optional PNG, contact sheet and GIF exports using Pillow.

The renderer and PNG encoder use the Python standard library. Pillow is needed
only when running the preview exporter. No media rendering process runs alongside
the daemon: production animations remain uploaded assets played by the device.

## Export an animation for review

Use a Python environment with Pillow installed:

```sh
python3 -m pip install Pillow
python3 preview_effort.py --out /tmp/effort-preview --levels high xhigh max ultra
```

This writes one PNG per level, `effort-levels.png` as a contact sheet, and a
synchronized `effort-levels.gif`. GIF timing follows the device animation:
45 frames at 25 fps, or 1.8 seconds. The output uses nearest-neighbor scaling,
preserves the pixel grid, and composites transparent overlays over black.
A shared GIF palette avoids independent color quantization on every frame.

Useful options:

```sh
# Preview a downward detent that replaces an already visible overlay.
python3 preview_effort.py --out /tmp/effort-change --direction down --transition change

# Export just one full-size 720×160 still of the actual frame at 640 ms.
python3 preview_effort.py --out /tmp/effort-still --levels ultra --scale 10 --frame 16 --format png
```

For a gallery or emulator that accepts PNG but cannot decode the device's native
animation codec, use the same frame renderer and the standard-library encoder:

```python
import effort_animation
from pixel_ui import encode_png

frame = effort_animation.frame('ultra', 16, entering=False)
png = encode_png(frame, effort_animation.W, effort_animation.H)
```

For native playback, pass `effort_animation.frames(...)` to
`animgen.encode_anim(...)` and keep `animgen.decode_check(...)` as an encoding
check. The shared canvas exports BGRA8888, exactly as this encoder expects.

## Adding another scene

Keep external data, device I/O and wall-clock scheduling outside the renderer.
Take explicit state and a frame index as inputs. Lay out text with `BitmapFont`,
paint the whole background with `Canvas.paint`, and place text with
`Canvas.draw_mask`; clipping also works during entrance and exit motion.
Use `SlideFade.at(..., entering=False)` for repeated input so a new value is
visible immediately. This avoids replaying a slow entrance for every detent.

Only draw a background plate when the design calls for one. Painting a continuous
background before the glyph mask preserves the same gradient between letters.
