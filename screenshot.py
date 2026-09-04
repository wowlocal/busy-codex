#!/usr/bin/env python3
"""Debug tool: capture either Busy Bar display to an upscaled PNG.

The /api/screen endpoint returns base64 of the raw framebuffer. The front
display is 72x16 in BGR byte order; the back display is 160x80 packed L4.

Usage: python3 screenshot.py [out.png] [front|back]
"""
import base64, http.client, os, struct, sys, urllib.parse, zlib

display = sys.argv[2] if len(sys.argv) > 2 else "front"
if display not in ("front", "back"):
    raise SystemExit("display must be 'front' or 'back'")

is_front = display == "front"
W, H, SCALE = (72, 16, 6) if is_front else (160, 80, 3)
path = f"/api/screen?display={0 if is_front else 1}"
source_ip = os.environ.get("BUSYBAR_USB_SOURCE_IP", "10.0.4.21")
connection = http.client.HTTPConnection(
    "10.0.4.20", timeout=5, source_address=(source_ip, 0),
)
connection.request("GET", path)
response = connection.getresponse()
if response.status != 200:
    raise SystemExit(f"screen request failed: HTTP {response.status}")
raw = base64.b64decode(response.read())
expected_size = W * H * 3 if is_front else W * H // 2
assert len(raw) == expected_size, len(raw)

out = sys.argv[1] if len(sys.argv) > 1 else "front.png"
rows = []
for y in range(H):
    row = bytearray()
    for x in range(W):
        if is_front:
            i = (y * W + x) * 3
            b, g, r = raw[i], raw[i + 1], raw[i + 2]
        else:
            packed = raw[(y * W + x) // 2]
            value = (packed >> 4 if x % 2 == 0 else packed & 0x0F) * 17
            r = g = b = value
        row += bytes((r, g, b)) * SCALE
    for _ in range(SCALE):
        rows.append(b"\x00" + bytes(row))

def chunk(tag, data):
    c = tag + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", W * SCALE, H * SCALE, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(b"".join(rows)))
png += chunk(b"IEND", b"")
open(out, "wb").write(png)
print("saved", out)
