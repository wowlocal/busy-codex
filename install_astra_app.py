#!/usr/bin/env python3
"""Install the standalone Astra Watch JavaScript app onto BUSY Bar over USB."""

from __future__ import annotations

import pathlib
import re
import time
import urllib.error
import urllib.parse

import animgen
from install_app import api, mkdir, png, write_file


APP_ID = "astra_watch_ai"
APP_ROOT = f"/ext/user_assets/{APP_ID}"
CANVAS_APP = "astra_watch_ai"
HERE = pathlib.Path(__file__).parent
SOURCE = HERE / "astra_device_app"


def compact_js(source: str) -> bytes:
    """Keep the script below the firmware's small storage-write body limit."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    lines = (
        line.strip() for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    )
    return ("\n".join(lines) + "\n").encode()


def replace_file(path: str, data: bytes):
    """Stage then rename because firmware cannot overwrite a loaded JS file."""
    staged = path + ".next"
    for candidate in (staged,):
        try:
            api("DELETE", "/storage/remove?path=" + urllib.parse.quote(candidate))
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
    write_file(staged, data)
    try:
        api("DELETE", "/storage/remove?path=" + urllib.parse.quote(path))
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise
    api("POST", "/storage/rename?path=" + urllib.parse.quote(staged)
        + "&new_path=" + urllib.parse.quote(path))
    print(f"replace {path} ({len(data)} bytes)")


def make_icons():
    green = (32, 192, 64)
    white = (230, 255, 235)
    black = (0, 0, 0)
    front = [
        "..xxx...",
        ".x...x..",
        ".x...x..",
        ".xxxxx..",
        ".x...x..",
        ".x...x..",
        "......o.",
        ".....o..",
    ]
    rows8 = []
    for line in front:
        row = []
        for char in line:
            row += list(green if char == "x" else white if char == "o" else black)
        rows8.append(row)

    back = [
        "....xxx....",
        "...x...x...",
        "...x...x...",
        "..x.....x..",
        "..xxxxxxx..",
        "..x.....x..",
        ".x.......x.",
        ".x.......x.",
        ".........o.",
        "........o..",
        "...........",
    ]
    rows11 = []
    for line in back:
        rows11.append([210 if char == "x" else 255 if char == "o" else 0
                       for char in line])
    return png(8, 8, rows8), png(11, 11, rows11, greyscale=True)


def main():
    mkdir(APP_ROOT)
    mkdir(APP_ROOT + "/appmeta")
    mkdir(APP_ROOT + "/scripts")
    script = compact_js((SOURCE / "scripts/main.js").read_text())
    if len(script) >= 8192:
        raise RuntimeError(f"compacted main.js is too large for the device: {len(script)} bytes")
    replace_file(APP_ROOT + "/scripts/main.js", script)

    front_icon, back_icon = make_icons()
    write_file(APP_ROOT + "/appmeta/icon_front_8x8.png", front_icon)
    write_file(APP_ROOT + "/appmeta/icon_back_11x11.png", back_icon)

    mkdir("/ext/apps_data/apps_menu")
    write_file("/ext/apps_data/apps_menu/js_apps_enabled", b"1")

    for filename, (generator, width, height, fps) in animgen.ASTRA_STATUS_ANIMS.items():
        frames = generator()
        blob = animgen.encode_anim(frames, fps=fps, w=width, h=height)
        animgen.decode_check(blob, frames, w=width, h=height)
        api("POST", f"/assets/upload?application_name={CANVAS_APP}&file={filename}", blob)
        print(f"upload Astra asset {filename} ({len(blob)} bytes)")

    # The scanner reacts to the manifest, so publish it after every dependency.
    try:
        api("POST", "/storage/write?path=" + urllib.parse.quote(
            APP_ROOT + "/appmeta/manifest.json"),
            (SOURCE / "appmeta/manifest.json").read_bytes(), retry=False)
        print("write Astra manifest.json")
    except Exception as exc:
        print(f"manifest write: {type(exc).__name__} (device may be rescanning)")
    time.sleep(3)
    listing = api("GET", "/storage/list?path=" + urllib.parse.quote(
        APP_ROOT + "/appmeta"))
    assert b"manifest.json" in listing, f"manifest missing: {listing!r}"
    print("Astra Watch manifest verified on device")
    print("\nDone. Open APPS and select Astra Watch.")


if __name__ == "__main__":
    main()
