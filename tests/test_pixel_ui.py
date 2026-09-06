import struct
import unittest
import zlib

from pixel_fonts import EFFORT_BOLD
from pixel_ui import BitmapFont, Canvas, SlideFade, encode_png


class PixelSceneTest(unittest.TestCase):
    def test_proportional_layout_has_exact_spacing_and_no_trailing_gap(self):
        font = BitmapFont({'I': ('1', '1'), 'L': ('10', '11')}, spacing=2)
        text = font.layout('IL')
        self.assertEqual((5, 2), (text.width, text.height))
        self.assertEqual({(0, 0), (0, 1), (3, 0), (3, 1), (4, 1)}, text.pixels)
        self.assertEqual(0, font.layout('').width)
        with self.assertRaisesRegex(ValueError, 'no glyph'):
            font.layout('X')

    def test_invalid_atlas_does_not_silently_corrupt_glyph_geometry(self):
        for glyphs in ({'I': ('1', '11')}, {'I': ('1x',)},
                       {'I': ('1',), 'L': ('1', '1')}, {}):
            with self.subTest(glyphs=glyphs), self.assertRaises(ValueError):
                BitmapFont(glyphs)

    def test_mask_clips_both_edges_without_overwriting_other_rows(self):
        font = BitmapFont({'X': ('111', '101', '111')})
        canvas = Canvas(2, 2)
        canvas.paint(lambda x, y: (10 + x, 20 + y, 30), alpha=99)
        canvas.draw_mask(font.layout('X'), -1, -1, (250, 240, 230), alpha=128)
        self.assertEqual(bytes((30, 20, 10, 99, 230, 240, 250, 128,
                                230, 240, 250, 128, 230, 240, 250, 128)),
                         canvas.to_bgra())

    def test_effort_glyphs_fit_actual_display_without_scaling(self):
        from effort_animation import H, LEVELS, W
        for level in LEVELS:
            mask = EFFORT_BOLD.layout(level.upper())
            self.assertEqual(12, mask.height)
            self.assertLessEqual(mask.width, W - 6)
            self.assertLessEqual(mask.height, H - 4)


class SlideFadeTest(unittest.TestCase):
    def test_fast_reveal_stable_hold_and_complete_exit(self):
        transition = SlideFade(frames=45, fade_frames=8)
        self.assertEqual((0, 6), transition.at(0))
        self.assertEqual((255, 0), transition.at(2))
        self.assertEqual((255, 0), transition.at(20))
        self.assertEqual((0, -3), transition.at(44))
        self.assertEqual(transition.at(44), transition.at(100))
        alphas = [transition.at(i)[0] for i in range(37, 45)]
        self.assertEqual(sorted(alphas, reverse=True), alphas)

    def test_repeated_input_is_visible_immediately_and_direction_is_mirrored(self):
        transition = SlideFade(frames=45, fade_frames=8)
        self.assertEqual((255, 0), transition.at(0, entering=False))
        for frame in range(45):
            up_alpha, up_offset = transition.at(frame, direction=1)
            down_alpha, down_offset = transition.at(frame, direction=-1)
            self.assertEqual(up_alpha, down_alpha)
            self.assertEqual(up_offset, -down_offset)


class PngTest(unittest.TestCase):
    def test_png_has_valid_chunks_exact_channels_and_preserved_alpha(self):
        bgra = bytes((30, 20, 10, 99, 230, 240, 250, 128))
        png = encode_png(bgra, 1, 2)
        self.assertEqual(b'\x89PNG\r\n\x1a\n', png[:8])
        offset, chunks = 8, {}
        while offset < len(png):
            size = struct.unpack('>I', png[offset:offset + 4])[0]
            kind = png[offset + 4:offset + 8]
            data = png[offset + 8:offset + 8 + size]
            crc = struct.unpack('>I', png[offset + 8 + size:offset + 12 + size])[0]
            self.assertEqual(zlib.crc32(kind + data) & 0xffffffff, crc)
            chunks[kind] = data
            offset += size + 12
        self.assertEqual((1, 2, 8, 6, 0, 0, 0), struct.unpack('>IIBBBBB', chunks[b'IHDR']))
        self.assertEqual(bytes((0, 10, 20, 30, 99, 0, 250, 240, 230, 128)),
                         zlib.decompress(chunks[b'IDAT']))
        self.assertIn(b'IEND', chunks)
        self.assertEqual(png, encode_png(bgra, 1, 2))

    def test_rejects_truncated_frame(self):
        with self.assertRaises(ValueError):
            encode_png(bytes(4), 72, 16)


if __name__ == '__main__':
    unittest.main()
