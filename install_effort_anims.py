#!/usr/bin/env python3
"""Generate, verify and upload only the BUSY Codex effort animations."""
import argparse
from pathlib import Path
import urllib.request
import animgen
import effort_animation
import daemon


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generate-only', action='store_true')
    parser.add_argument('--out', default='anims')
    args = parser.parse_args()
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    transport = daemon.make_transport() if not args.generate_only else None
    assets = [('effort_clear.anim', [bytes(effort_animation.W * effort_animation.H * 4)])]
    for level in effort_animation.LEVELS:
        for direction in (1, -1):
            for entering in (True, False):
                assets.append((effort_animation.filename(level, direction, entering),
                               effort_animation.frames(level, direction, entering=entering)))
    for name, frames in assets:
        blob = animgen.encode_anim(frames, fps=effort_animation.FPS)
        animgen.decode_check(blob, frames)
        (folder / name).write_bytes(blob)
        if transport:
            request = urllib.request.Request(
                transport.base + '/assets/upload?application_name=' + daemon.APP_NAME + '&file=' + name,
                data=blob, method='POST', headers=transport.headers)
            with transport.opener.open(request, timeout=20) as response:
                response.read()
        print(f'{name}: {len(blob)} bytes' + (' uploaded' if transport else ''), flush=True)


if __name__ == '__main__':
    main()
