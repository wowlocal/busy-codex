import importlib.util
import tempfile
import unittest
from pathlib import Path

import effort_animation as animation
from preview_effort import export_preview


class NativeFrameTest(unittest.TestCase):
    def test_random_access_matches_native_sequence_and_codec(self):
        import animgen
        for level in animation.LEVELS:
            for direction in (1, -1):
                with self.subTest(level=level, direction=direction):
                    frames = animation.frames(level, direction, entering=False)
                    self.assertEqual(animation.FRAMES, len(frames))
                    for index in (0, 1, 20, animation.FRAMES - 1):
                        self.assertEqual(frames[index], animation.frame(
                            level, index, direction, entering=False))
                        self.assertEqual(72 * 16 * 4, len(frames[index]))
                    animgen.decode_check(animgen.encode_anim(frames), frames)


@unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow is an optional preview dependency')
class PreviewExportTest(unittest.TestCase):
    def test_export_matches_native_pixels_and_gif_timing(self):
        from PIL import Image, ImageSequence
        with tempfile.TemporaryDirectory() as directory:
            paths = export_preview(Path(directory), ['high', 'ultra'], scale=2)
            self.assertEqual(4, len(paths))
            native = animation.frame('high', 16)
            with Image.open(Path(directory) / 'effort-high.png') as image:
                self.assertEqual((144, 32), image.size)
                for y in range(animation.H):
                    for x in range(animation.W):
                        offset = (y * animation.W + x) * 4
                        b, g, r, _ = native[offset:offset + 4]
                        for dx, dy in ((0, 0), (1, 1)):
                            self.assertEqual((r, g, b), image.getpixel((x * 2 + dx, y * 2 + dy)))
            with Image.open(Path(directory) / 'effort-levels.png') as image:
                self.assertEqual((144, 72), image.size)
            with Image.open(Path(directory) / 'effort-levels.gif') as image:
                self.assertEqual((144, 72), image.size)
                self.assertEqual(0, image.info['loop'])
                self.assertEqual(1800, sum(frame.info['duration']
                                          for frame in ImageSequence.Iterator(image)))
            # No random seed, wall clock, platform font, or timestamps enter
            # generated media: identical inputs give identical review assets.
            before = {path.name: path.read_bytes() for path in paths}
            export_preview(Path(directory), ['high', 'ultra'], scale=2)
            self.assertEqual(before, {path.name: path.read_bytes() for path in paths})


if __name__ == '__main__':
    unittest.main()
