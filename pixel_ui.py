"""Small, dependency-free primitives for native-resolution display scenes.

Colors are RGB at the drawing boundary; frames are BGRA8888 for ``animgen``.
There is no device I/O or scheduling here, so a scene can be rendered offline
or used by another integration without starting the daemon.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Callable, Mapping
import zlib

RGB = tuple[int, int, int]


def encode_png(bgra: bytes, width: int, height: int) -> bytes:
    """Encode a native display frame as lossless RGBA PNG without Pillow.

    This is also useful for gallery/emulator previews, which may not implement
    every on-device .anim codec. Alpha is preserved for overlay compositing.
    """
    if width <= 0 or height <= 0 or len(bgra) != width * height * 4:
        raise ValueError('Frame length does not match its dimensions')

    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # PNG filter: none
        for i in range(y * stride, (y + 1) * stride, 4):
            b, g, r, a = bgra[i:i + 4]
            raw.extend((r, g, b, a))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


@dataclass(frozen=True)
class PixelMask:
    width: int
    height: int
    pixels: frozenset[tuple[int, int]]


class BitmapFont:
    """Lay out proportional binary glyphs without scaling or smoothing."""

    def __init__(self, glyphs: Mapping[str, tuple[str, ...]], spacing: int = 1):
        if not glyphs or spacing < 0:
            raise ValueError('A font needs glyphs and nonnegative spacing')
        self.spacing = spacing
        self.height = len(next(iter(glyphs.values())))
        self._glyphs = {}
        for letter, rows in glyphs.items():
            if (len(letter) != 1 or not rows or not rows[0]
                    or len(rows) != self.height
                    or any(len(row) != len(rows[0]) or set(row) - {'0', '1'}
                           for row in rows)):
                raise ValueError(f'Invalid bitmap glyph: {letter!r}')
            self._glyphs[letter] = PixelMask(
                len(rows[0]), self.height,
                frozenset((x, y) for y, row in enumerate(rows)
                          for x, bit in enumerate(row) if bit == '1'),
            )

    def layout(self, text: str) -> PixelMask:
        pixels = set()
        offset = 0
        for letter in text:
            try:
                glyph = self._glyphs[letter]
            except KeyError:
                raise ValueError(f'Font has no glyph for {letter!r}') from None
            pixels.update((offset + x, y) for x, y in glyph.pixels)
            offset += glyph.width + self.spacing
        return PixelMask(max(0, offset - self.spacing), self.height,
                         frozenset(pixels))


class Canvas:
    """A clipped pixel canvas whose output can be passed directly to animgen."""

    def __init__(self, width: int, height: int):
        if width <= 0 or height <= 0:
            raise ValueError('Canvas dimensions must be positive')
        self.width, self.height = width, height
        self._pixels = bytearray(width * height * 4)

    def paint(self, color_at: Callable[[int, int], RGB], alpha: int = 255):
        """Fill the scene, including spaces between foreground glyphs."""
        for y in range(self.height):
            for x in range(self.width):
                r, g, b = color_at(x, y)
                index = (y * self.width + x) * 4
                self._pixels[index:index + 4] = bytes((b, g, r, alpha))

    def draw_mask(self, mask: PixelMask, x: int, y: int,
                  color: RGB, alpha: int = 255):
        """Replace covered pixels; off-screen pixels are clipped, not wrapped."""
        r, g, b = color
        pixel = bytes((b, g, r, alpha))
        for mx, my in mask.pixels:
            px, py = x + mx, y + my
            if 0 <= px < self.width and 0 <= py < self.height:
                index = (py * self.width + px) * 4
                self._pixels[index:index + 4] = pixel

    def to_bgra(self) -> bytes:
        return bytes(self._pixels)


def smoothstep(value: float) -> float:
    value = max(0., min(1., value))
    return value * value * (3 - 2 * value)


@dataclass(frozen=True)
class SlideFade:
    """An overlay envelope that can skip its entrance on repeated input.

    Timings use frame indices, so file generation and previews use the same
    deterministic clock. The last frame is completely transparent.
    """

    frames: int
    fade_frames: int
    reveal_frames: int = 2
    slide_frames: int = 3
    enter_distance: int = 6
    leave_distance: int = 3

    def __post_init__(self):
        if (self.frames < 2 or not 2 <= self.fade_frames <= self.frames
                or self.reveal_frames <= 0 or self.slide_frames <= 0):
            raise ValueError('Transition timing must have a positive duration')

    def at(self, frame: int, direction: int = 1,
           entering: bool = True) -> tuple[int, int]:
        """Return (alpha, horizontal offset) for this display frame."""
        frame = max(0, frame)
        outro = smoothstep((frame - (self.frames - self.fade_frames))
                           / (self.fade_frames - 1))
        intro = max(0, 1 - frame / self.slide_frames) ** 3 if entering else 0
        reveal = smoothstep(frame / self.reveal_frames) if entering else 1
        alpha = round(255 * reveal * (1 - outro))
        offset = round(direction * (self.enter_distance * intro
                                    - self.leave_distance * outro))
        return alpha, offset
