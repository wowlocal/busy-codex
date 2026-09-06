#!/usr/bin/env python3
"""Export effort scenes as pixel-exact PNGs and a synchronized GIF/contact sheet.

Pillow is an optional development dependency, imported only when exporting.
The daemon and on-device animation playback do not use it. No device, Codex
session, credentials, or network connection is needed.

Example:
    python3 preview_effort.py --out /tmp/effort --levels high xhigh max ultra
"""
from __future__ import annotations

import argparse
from pathlib import Path

import effort_animation as animation


def export_preview(output: Path, levels: list[str], scale: int = 8,
                   direction: int = 1, entering: bool = True,
                   still_frame: int = 16, formats: str = 'all') -> list[Path]:
    """Export the same frames the native .anim encoder consumes.

    All resizing is nearest-neighbor. GIFs share one palette across all frames
    to avoid changing colors as the quantizer encounters different effects.
    The transparency of the native overlay is composited over black.
    """
    from PIL import Image

    if not levels or any(level not in animation.LEVELS for level in levels):
        raise ValueError('Choose at least one supported effort level')
    if scale < 1 or not 0 <= still_frame < animation.FRAMES:
        raise ValueError('Scale must be positive and still frame in range')
    if formats not in ('png', 'gif', 'all'):
        raise ValueError('Format must be png, gif or all')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    result = []
    width, height = animation.W, animation.H
    gap = 4
    sheet_size = (width, len(levels) * height + (len(levels) - 1) * gap)

    def scene(level, index):
        data = animation.frame(level, index, direction, entering)
        rgba = Image.frombytes('RGBA', (width, height), data, 'raw', 'BGRA')
        image = Image.new('RGB', rgba.size, 'black')
        image.paste(rgba, mask=rgba.getchannel('A'))
        return image

    def sheet(index):
        image = Image.new('RGB', sheet_size, (12, 15, 21))
        for row, level in enumerate(levels):
            image.paste(scene(level, index), (0, row * (height + gap)))
        return image

    def zoom(image):
        return image.resize((image.width * scale, image.height * scale),
                            Image.Resampling.NEAREST)

    if formats in ('png', 'all'):
        for level in levels:
            path = output / f'effort-{level}.png'
            zoom(scene(level, still_frame)).save(path)
            result.append(path)
        path = output / 'effort-levels.png'
        zoom(sheet(still_frame)).save(path)
        result.append(path)

    if formats in ('gif', 'all'):
        images = [sheet(index) for index in range(animation.FRAMES)]
        # Native-resolution samples keep palette generation small regardless
        # of the requested export scale. MEDIANCUT is deterministic.
        samples = images[::5]
        atlas = Image.new('RGB', (sheet_size[0], sheet_size[1] * len(samples)))
        for row, sample in enumerate(samples):
            atlas.paste(sample, (0, row * sheet_size[1]))
        palette = atlas.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        encoded = [zoom(image.quantize(palette=palette, dither=Image.Dither.NONE))
                   for image in images]
        path = output / 'effort-levels.gif'
        encoded[0].save(path, save_all=True, append_images=encoded[1:],
                        duration=round(1000 / animation.FPS), loop=0,
                        optimize=False, disposal=2)
        result.append(path)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='Output directory')
    parser.add_argument('--levels', nargs='+', choices=animation.LEVELS,
                        default=['high', 'xhigh', 'max', 'ultra'])
    parser.add_argument('--scale', type=int, default=8, help='Integer pixel scale')
    parser.add_argument('--direction', choices=('up', 'down'), default='up')
    parser.add_argument('--transition', choices=('enter', 'change'), default='enter')
    parser.add_argument('--frame', type=int, default=16, help='PNG frame index')
    parser.add_argument('--format', choices=('png', 'gif', 'all'), default='all')
    args = parser.parse_args()
    try:
        paths = export_preview(args.out, args.levels, args.scale,
                               1 if args.direction == 'up' else -1,
                               args.transition == 'enter', args.frame, args.format)
    except ImportError as error:
        if error.name != 'PIL':
            raise
        parser.error('Preview export needs Pillow: python3 -m pip install Pillow')
    except ValueError as error:
        parser.error(str(error))
    for path in paths:
        print(path.resolve())


if __name__ == '__main__':
    main()
