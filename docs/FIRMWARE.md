# BUSY Bar firmware notes

[Back to BUSY Codex](../README.md) · [Extension guide](EXTENDING.md)

Historical hardware observations, primarily from firmware 1.1.1.
Behavior can differ on later firmware; these notes are not requirements for the standalone app.

Things discovered the hard way, verified on-device:

- `rectangle` elements have an **undocumented `border_width`** defaulting
  to a 1px *white* border — thin rectangles render pure white unless you
  send `border_width: 0`.
- `/api/screen` returns the framebuffer **base64-encoded in BGR order**
  (`screenshot.py` handles it).
- The `small` font is **proportional** (~3.8px digits); measure on-device
  before doing pixel layout.
- The `.anim` format (`bicycle0`): BGRA8888/BGR888/Gray4 + RLE +
  inter-frame collapsing + named sections. `animgen.py` implements a
  compatible encoder with a decode round-trip check.
- Writing `manifest.json` or binary data into
  `/ext/user_assets/<app>/appmeta/` **crashes and reboots** the firmware
  (half-finished JS-app scanner). Theme dirs under
  `/ext/apps_assets/busy/themes/` are safe.
- While a focus session is **running**, all canvas drawing is rejected —
  even at priority 100 (docs say sessions sit at 90; not on 1.1.1).
- Sessions can be controlled via `PUT /api/busy/snapshot`
  (`card_id`, `is_paused`, `snapshot_timestamp_ms` required; `type:
  NOT_STARTED` ends one). The two physical mode keys map to
  `/api/busy/profiles/{busy|custom}`.
- `storage` API: write = POST raw body, remove = **DELETE**, rename takes
  `path` + `new_path`.
- A running JS app keeps `scripts/main.js` open. The Astra installer compacts
  the source below the firmware request limit and stages `main.js.next` before
  replacing the entry file; exit/restart the app before installing an update.
- Re-uploading an `.anim` that is currently being played fails with
  "Failed to open file for writing" — clear the element (freeing the file
  handle) before uploading.
