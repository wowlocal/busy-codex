"""The package builder may never write through links outside its destination."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class GalleryBuilderTests(unittest.TestCase):
    def test_rejects_output_and_nested_symlinks_without_touching_targets(self):
        for linked in ("directory", "SOURCE.json", "assets"):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                outside = base / "outside"
                outside.mkdir()
                sentinel = outside / "keep.txt"
                sentinel.write_text("must not change")
                destination = base / "busy-codex"
                if linked == "directory":
                    destination.symlink_to(outside, target_is_directory=True)
                else:
                    destination.mkdir()
                    if linked == "SOURCE.json":
                        (destination / linked).symlink_to(sentinel)
                    else:
                        (destination / "SOURCE.json").write_text('{"files": {}}')
                        (destination / linked).symlink_to(outside, target_is_directory=True)
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "build_gallery.py"), "--output", str(destination)],
                    capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("symlink", result.stderr)
                self.assertEqual(sentinel.read_text(), "must not change")
                self.assertEqual(list(outside.iterdir()), [sentinel])


if __name__ == "__main__":
    unittest.main()
