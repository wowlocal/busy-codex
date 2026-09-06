#!/usr/bin/env python3
"""Build a self-contained gallery folder from the reviewed busy-codex sources.

    python3 scripts/build_gallery.py --output /tmp/busybar-apps/apps/busy-codex

This only writes the explicitly selected local output directory. It never
downloads dependencies, contacts a device, installs services or publishes code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
SOURCES = (
    "daemon.py", "display_scene.py", "busybar_input.py", "busybar_http.py", "ai_status.py",
    "codex_effort.py", "codex_focus.py", "codex_target.py", "codex_usage.py",
    "codex_cli_native.py", "codex_cli_client.py", "report.py",
    "adapters/codex_status.py", "effort_animation.py", "pixel_ui.py", "pixel_fonts.py",
)
STATE_ASSETS = ("work.anim", "work_fast.anim", "astra.anim", "think.anim",
                "done.anim", "wait.anim", "error.anim", "idle.anim")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="destination named busy-codex; existing files may be overwritten")
    args = parser.parse_args(argv)
    requested = args.output.expanduser().absolute()
    # Canonicalize the explicitly chosen parent (on macOS /tmp itself points
    # to /private/tmp), but never follow the destination folder or its contents.
    out = requested.parent.resolve() / requested.name
    if out.is_symlink():
        parser.error("output directory must not be a symlink")
    if out.name != "busy-codex" or out == ROOT or ROOT in out.parents:
        parser.error("--output must be an external directory named busy-codex")
    if out.exists() and not out.is_dir():
        parser.error("output must be a directory")
    if out.exists() and any(path.is_symlink() for path in out.rglob("*")):
        parser.error("output contains symlinks; refusing to clean or write through them")
    if out.exists() and any(out.iterdir()):
        # Only refresh directories this builder previously created. Never
        # recursively delete a path chosen by a user on the strength of its name.
        if not (out / "SOURCE.json").is_file():
            parser.error("non-empty output is not a previous build (SOURCE.json missing)")
        previous = json.loads((out / "SOURCE.json").read_text())
        for name in previous.get("files", {}):
            path = (out / name).resolve()
            if out in path.parents and path.is_file():
                path.unlink()
    out.mkdir(parents=True, exist_ok=True)
    files = {}

    def copy(source, target):
        destination = out / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        files[target] = digest(destination)

    for relative in SOURCES:
        copy(ROOT / relative, relative)
    copy(ROOT / "run_app.py", "app.py")
    copy(ROOT / "LICENSE", "LICENSE")
    metadata = ROOT / "gallery" / "busy-codex"
    for name in ("README.md", "manifest.yaml", "preview.gif"):
        if (metadata / name).is_file():
            copy(metadata / name, name)
        elif name != "preview.gif":
            parser.error(f"missing gallery metadata: {name}")

    sys.path.insert(0, str(ROOT))
    import animgen
    import effort_animation

    def asset(name, frames, fps, width=72, height=16):
        blob = animgen.encode_anim(frames, fps=fps, w=width, h=height)
        animgen.decode_check(blob, frames, w=width, h=height)
        destination = out / "assets" / name
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(blob)
        files["assets/" + name] = digest(destination)

    for name in STATE_ASSETS:
        generate, width, height, fps = animgen.ANIMS[name]
        asset(name, generate(), fps, width, height)
    asset("effort_clear.anim", [bytes(72 * 16 * 4)], effort_animation.FPS)
    for level in effort_animation.LEVELS:
        for direction in (1, -1):
            for entering in (True, False):
                asset(effort_animation.filename(level, direction, entering),
                      effort_animation.frames(level, direction, entering=entering),
                      effort_animation.FPS)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True))
    provenance = {"repository": "https://github.com/wowlocal/busy-codex",
                  "revision": revision, "working_tree_changes": dirty,
                  "generator": "scripts/build_gallery.py", "files": dict(sorted(files.items()))}
    (out / "SOURCE.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"Built {len(files)} files in {out}")
    print("Generate preview with: npm run preview -- busy-codex --seconds 7.2 --fps 9 -- --demo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
