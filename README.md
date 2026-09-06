# busybar-claude-status

Turn a [BUSY Bar](https://busy.app/) into a live status display for
[Claude Code](https://claude.com/claude-code): session state, model,
reasoning effort, context-window usage and plan rate limits — rendered on
the 72×16 front LED matrix with buttery-smooth native animations.

[中文文档 / Chinese docs](README.zh-CN.md)

**Avatar style** — a faithful pixel Clawd acts out the session state:

![Avatar style](docs/img/avatar-working.png)

**Minimal style** — everything visible at once:

![Minimal style](docs/img/working.png)

```
############################    1px per-pixel animated ring (.anim, 25 fps)
#  Fable 5 max      [##----] #  model + effort · progress toward weekly reset
#  W [########---] A  WORK   #  weekly quota · Astra rollout · state word
############################
```

## What it shows

| Element | Meaning |
| --- | --- |
| **Ring animation** | Session state, played natively by the firmware's own `.anim` decoder (same one as the built-in *keep out* theme): rainbow marquee = WORKING, purple wave = THINKING, green breathing = COMPLETE, orange pulse = WAIT (+ status LED), red blink = ERROR/FAILED, dim gray = IDLE |
| **Model + effort** | e.g. `Fable 5 max`, colored with Claude Code's own theme palette per `/effort` level (`inactive` gray / `permission` blue / `warning` yellow / `fastMode` orange / `effortUltra` purple) |
| **Reset progress** | Small top-right bar fills as the seven-day window approaches its reset; green → yellow → orange → red |
| **Plan usage** | Large `W` bar shows weekly quota remaining; it shrinks and changes from green to yellow/orange/red as capacity runs low |
| **Astra rollout** | Tiny pixel `A` beside the `W` bar: gray waiting, amber hidden, green available, red stale/error; availability starts a fast rainbow celebration around the display |
| **State word** | `THINK / WORK / WAIT / ERR / FAIL / DONE / IDLE` |

![Ring only](docs/img/ring-only.png)

### GPT-6 Astra rollout indicator

With the personal `astra-watch` Codex plugin installed, the main agent screen
reads its non-secret state from `~/.local/state/astra-watch/state.json`. A 3x5
pixel `A` fits between the weekly quota gauge and the state word without taking
space from the model label. When Astra becomes selectable, the `A` turns green
and the normal agent contour becomes a fast rainbow celebration with five
white-hot orbiting sparks.

Override the state path with `BUSYBAR_ASTRA_STATE`.
`BUSYBAR_ASTRA_STALE_S` controls when an unrefreshed result turns red (default:
1800 seconds). If the watcher is not installed, the indicator remains
transparent and the existing display is unchanged.

### AI provider outage overlay

An optional network-only monitor polls the open-source
[AIWatch](https://github.com/bentleypark/aiwatch) public API. It groups related
surfaces into seven providers: OpenAI (API, ChatGPT, Codex), Anthropic (API,
claude.ai, Claude Code), Gemini, OpenRouter, DeepSeek, Mistral and Perplexity.

X.com is monitored separately through the open-source
[isUpMap](https://github.com/Jaironlanda/isupmap) API. It combines a direct
availability check with a community-report surge signal, the closest open-source
equivalent to Downdetector in this setup.

Google.com is checked directly through Google's lightweight
`/generate_204` connectivity endpoint. Two consecutive failures are required
before a red `GOOGLE / DOWN / WEB` alert appears, avoiding one-off timeout
flicker.

While everything is operational the overlay owns no pixels, so the agent
dashboard stays visible. A degraded or down provider takes over with a separate
priority-80 canvas, an amber/red animated contour, provider name, affected
surface and position in the rotation. Incidents appear first; Anthropic, X.com
and Google.com are always appended to an active rotation and shown with a green
`OK` state when healthy. xAI/Grok and GitHub Copilot are deliberately excluded.
Items rotate every four seconds; stale or unavailable monitoring data is never
presented as an outage. No Codex logs, browser automation, API key or persistent
status file is used.

The physical controls work while the overlay is visible: turn the encoder for
the previous/next service, press `START` for next, or press `OK` to refresh all
sources and return to the first item. Manual selection pauses auto-rotation for
one four-second card interval. `BACK` remains the firmware's system-level exit
key. Input comes directly from the device's local status WebSocket.

Enable it in `env.sh`:

```bash
export BUSYBAR_AI_STATUS=1
# Optional: BUSYBAR_AI_STATUS_POLL_S=60
# Optional: BUSYBAR_AI_STATUS_URL=https://.../api/v1/status
# Optional: BUSYBAR_X_STATUS_URL=https://.../api/status
# Optional: BUSYBAR_GOOGLE_STATUS_URL=https://www.google.com/generate_204
```

## How it works

```
statusline command --.                                    USB (10.0.4.20)
                     +--> daemon.py :8765 ---------------> BUSY Bar
settings.json hooks -'      |  session store               /api/display/draw
                            +--> GET /status               (pre-uploaded .anim
                                 (future on-device app)     assets, native fps)
```

- Claude Code's **statusline** JSON (model, effort, context window, rate
  limits) and **hook events** (UserPromptSubmit, Pre/PostToolUse, Stop,
  PermissionRequest, …) are forwarded to a tiny local daemon.
- The daemon keeps per-session state (multiple Claude sessions supported,
  even across several computers — the one you last talked to wins) and
  renders to the device over the HTTP API. Ring animations are **pre-rendered `.anim` files** generated by
  `animgen.py` — a from-scratch Python encoder for the firmware's
  undocumented `bicycle0` animation format — uploaded once and played by
  the device itself, so the animation is perfectly smooth with near-zero
  traffic.
- Everything is Python 3 stdlib. No dependencies.

**Not just Claude:** the daemon core is provider-agnostic. Codex, Cursor,
CI jobs — anything that can run one curl — can drive the display through
`POST /v1/report`. Claude-specific semantics (effort colors, 5h/7d plan
windows) live in a built-in adapter. Wi-Fi and cloud transports are
selectable via `BUSYBAR_TRANSPORT`; a BLE transport is designed. See
**[docs/EXTENDING.md](docs/EXTENDING.md)**.

## Install

Requirements: macOS, Linux or Windows; Python 3.9+; a BUSY Bar
connected over USB (firmware 1.1.x); Claude Code with statusline +
hooks support. On Windows use `py`/`python` instead of `python3` —
every entry point resolves the interpreter via `sys.executable`, and
the glue layer (`report.py`, `adapters/codex_notify.py`) is pure Python
with no bash/nohup/pgrep dependencies. (`report.sh` remains for
existing POSIX installs.) Verified on a real Windows machine as a hub
client (hooks + statusline forwarded over Wi-Fi, see below); running
the daemon itself on Windows with the Bar on its USB port is untested —
issues welcome.

```bash
git clone https://github.com/Alpharius-003/busybar-claude-status
cd busybar-claude-status

python3 animgen.py anims/                 # generate ring animations
python3 - <<'PY'                          # upload them to the device
import animgen, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for app, animations in (
    ("claude_status", animgen.ANIMS),
    ("ai_provider_status", animgen.AI_STATUS_ANIMS),
    ("astra_watch_ai", animgen.ASTRA_STATUS_ANIMS),
):
    for f, (gen, w, h, fps) in animations.items():
        frames = gen()
        blob = animgen.encode_anim(frames, fps=fps, w=w, h=h)
        opener.open(urllib.request.Request(
            f"http://10.0.4.20/api/assets/upload?application_name={app}&file={f}",
            data=blob, method="POST"), timeout=15)
        print("uploaded", app, f)
PY

python3 setup_claude.py install           # wire into Claude Code (backs up first)
python3 install_astra_app.py              # optional Astra Watch entry in APPS
```

Start a Claude Code session — the daemon auto-spawns on the first
statusline refresh and the display appears. `setup_claude.py uninstall`
reverses everything.

## Codex weekly usage

Weekly usage comes from Codex's documented
[`account/rateLimits/read`](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt)
endpoint, refreshed every 60 seconds by the background adapter even when the
selected task is idle. It reads the account's `codex` bucket from
`rateLimitsByLimitId`; task history and other models' quota buckets cannot
replace it. Windows are identified by duration, so the week can be either
`primary` or `secondary` and a lone five-hour window never becomes a week.

The large **W** bar is capacity remaining (`100 − usedPercent`). The small upper
bar is elapsed time toward the API's actual weekly reset, not calendar-week
progress or task context usage. A reset triggers an early account refresh.
Expired data never implies 100% remaining: both fills clear and **?** appears
until fresh data arrives. On a temporary read failure, the last confirmed
snapshot remains usable for at most three minutes and never past its reset.

The installed Codex executable handles authentication through the existing
ChatGPT login. Each poll only initializes an app-server and reads account
limits, then closes it. It does not start a task, call a model, or consume a
reset credit. The Desktop-bundled executable is preferred on macOS, then
`codex` on PATH; `BUSYBAR_CODEX_BIN` can override its location. Set
`BUSYBAR_CODEX_LIMIT_ID` only to intentionally display another account bucket.

`GET /status` exposes `quota_status` (source, bucket, freshness, timestamp and
error), each window's `observed_at`/`valid_until`, and `week_progress_pct`.
For a fresh one-off diagnostic, run `python3 adapters/codex_status.py --once -v`.
Notify hooks use `--no-usage-refresh` so frequent turns do not multiply account
requests or overwrite the background reader's usage data.

## Codex effort dial (Desktop and CLI)

The encoder follows the foreground app: the task open in Codex Desktop's
primary window, or the focused terminal running a connected Codex CLI.
Clockwise increases effort, counterclockwise decreases it; the ends clamp.
Supported levels come from that model's Codex catalog (the CLI's live
`model/list` response, or Desktop's local cache). Quick turns are
combined, and only a confirmed settings change triggers the blue animation.
The new effort applies to subsequent turns; it does not interrupt a running
answer or send a message. Bold, 12-pixel lettering slides into place over moving
blue waves. The overlay fades in over the live quota screen and dissolves back
after 2.6 seconds; successive dial turns keep the background visible.
Higher effort subtly increases wave speed and glint intensity and adds a violet
tint. The gradient stays continuous behind and around the lettering.

The controller retains each model's last confirmed catalog entry for up to five
minutes if the shared cache becomes unreadable or temporarily loses that model.
A fresh valid entry takes precedence immediately. Catalog errors preserve the
Desktop connection and log the exact model and available entries for diagnosis.

![Native effort animation](docs/img/effort-ultra.png)

Generate and upload the native 25 fps wave, glint and sliding-label animations:

```bash
python3 install_effort_anims.py
```

Control is enabled by default. Run the daemon and Codex adapter on the same Mac
as Codex Desktop. Selection follows the app's `thread_stream_view_activity_changed`
lifecycle events in `~/Library/Logs/com.openai.codex`, which record task-view
mounts and unmounts. Background model output and auto-review sessions cannot
select a task. The adapter follows the same selection and refreshes on tab
changes even when the selected task is idle.

For CLI control, connect the ordinary `codex` command once from this repository:

```bash
python3 install_codex_cli.py install
codex --yolo
# Or continue an existing CLI task:
codex resume <session-id> --yolo
```

The installer puts a small dispatcher at `~/.local/bin/codex` and preserves
the original executable or symlink beside it. Interactive startup, resume and
fork connect automatically. Commands such as `exec`, `app-server`, `login`,
`update`, help/version, explicit remote connections and calls without a TTY
go directly to the original CLI. Arguments such as `--yolo`, model and config
overrides are preserved. `BUSYBAR_CODEX_LAUNCH=0 codex ...` bypasses the bridge;
`python3 install_codex_cli.py uninstall` restores the original command.
Existing terminal windows can use the same `codex` command immediately after
their currently running CLI exits; no shell configuration reload is needed.

The bridge uses your existing configuration and login, and starts the BUSY Bar
daemon/adapter as needed. As an alternative to installing the dispatcher,
`python3 codex_cli.py [args]` (or the `busy-codex-cli` symlink) launches it explicitly.
`BUSYBAR_CODEX_CLI_BIN` can select another
executable. A recent CLI with `--remote unix://` and `thread/settings/update`
is required; this was verified with Codex 0.153.4. Existing standalone CLI
processes must be exited and resumed through the launcher once, because their
embedded server has no external settings connection.

The launcher connects the TUI and dial to one local app-server through private
Unix sockets. It forwards the TUI protocol, changes only effort, preserves
collaboration mode, and waits for `thread/settings/updated` before showing
success. Only session/settings/focus metadata is saved under
`$CODEX_HOME/busybar-cli`; the bridge does not save prompts or answers.
Normal CLI startup, resume and fork select the corresponding CLI task.
If the CLI loads several task views (for example through its agent picker),
control pauses: cached view switches are not exposed by the server protocol.
Explicitly resuming a task restores an unambiguous target.

On macOS, application identity comes from a fresh foreground-process lookup, with terminal tab/pane
focus supplied by the terminal's focus reports. Ghostty, Terminal, iTerm2,
WezTerm, Kitty, Alacritty and Warp are recognized. Multiplexers must forward
focus reports. If several launchers report focus, the dial pauses until one is
unambiguous. On Linux it uses the single focused launcher. Switching to another
app on macOS disables dial writes while keeping the last task and account
usage visible. No Accessibility or Screen Recording permission is required.

For an optional fixed task, set this in `env.sh` before starting both processes
(manual launches should first source the file):

```bash
export BUSYBAR_CODEX_THREAD_ID="<existing local task UUID>"
```

`BUSYBAR_CODEX_THREAD_ID` pins the display/adapter and dial to that task. Without
it, selection is automatic. Home/Settings pages, a closed Codex app, or missing
view events disable writes. Keep tasks in one primary Codex window: if multiple
primary windows have visible tasks, control pauses because focus can change
without a log event. No UI automation, Accessibility permission, app patching,
or new Codex conversation is needed.

The integration uses the installed Desktop's private, versioned IPC protocol
(snapshot v11, settings request v1), verified against the September 2026 app.
It preserves the model, collaboration mode and other settings. It fails closed
when the app disconnects, ownership or protocol changes, or the model catalog
does not contain the current effort and no recent confirmed entry is available.
Menus, Astra Watch and provider outage
overlays retain their controls. Remote hosts and CLI processes started without
the launcher are not controlled.
`GET /hub` includes `codex_target` (Desktop/CLI selection), `codex_focus`
(Desktop view evidence) and `codex_effort` connection,
target, model, effort and errors. `BUSYBAR_CODEX_LOG_DIR` overrides the Desktop
log directory. `BUSYBAR_CODEX_IPC` overrides the socket path; `BUSYBAR_CODEX_EFFORT=0` disables
the controller without disabling the quota display.

`codex_target.foreground_bundle` shows the app observed by the background
process. `device_input.last_encoder` records the latest dial event, whether it
was accepted and why it was ignored. macOS foreground polling works without an
AppKit event loop, including after locking/unlocking or changing apps.

## Display styles

Two looks, one codebase — pick with `BUSYBAR_STYLE` (persist it in an
`env.sh` next to `daemon.py`, e.g. `export BUSYBAR_STYLE=avatar`):

- **`minimal`** (default) — the layout above: state word + weekly gauges always
  visible.
- **`avatar`** — a pixel companion (a 1:1 recreation of the Claude Code
  terminal mascot) acts out the state on the right: typing at a laptop
  while WORKING (with blinks), light bulb while THINKING, coffee break
  when DONE, X-eyes on ERROR, zzz when idle — plus a vertical context
  gauge. The bottom-left slot shows the state as a word and swaps to
  quotas once the work is done.

For Codex `fast` mode, the separate badge is replaced by a yellow animated
working contour so it cannot collide with text on the 72×16 display.

Styles are a runtime option, not separate branches — every release
contains both.

## Display modes

Set `BUSYBAR_RENDER_MODE` (or edit `RENDER_MODE` in `daemon.py`):

- **`auto`** (default) — display whenever an agent is active; after 10
  minutes of idle the screen is handed back to the device and returns on
  the next activity (`BUSYBAR_IDLE_CLEAR_S` tunes this; `0` = keep the
  display forever).
- **`theme`** — manual, on the device: the display only shows while
  **"claude" is the currently selected BUSY/CUSTOM theme**. Install the
  theme with `python3 install_theme.py` — a breathing claude-orange ring
  with the companion typing in the middle; it appears in the device's
  theme picker (also the screen during a claude-theme focus session):

  ![Claude theme](docs/img/claude-theme.png)

  Picking it toggles the display on, picking another theme toggles it
  off. (In `auto` mode the theme is unrelated to the status display —
  it's just a theme.) `python3 claude_card.py install` binds the physical
  CUSTOM key to it (backs up your current card; `restore` undoes).
- **`off`** — data bridge only (`GET /status` on `127.0.0.1:8765` and the
  USB interface for the future on-device app).

## Several computers, one Bar

Claude Code on a Mac *and* a Windows PC (any number of sessions each),
one display that follows you. The computer the Bar is plugged into runs
the daemon as the **hub**; every other computer runs nothing — its hooks
and statusline are forwarded to the hub over the LAN.

```bash
# on the computer with the Bar (the hub)
python3 setup_claude.py install --lan

# on every other computer (Windows: py setup_claude.py ...)
python3 setup_claude.py install --hub http://<hub-name>.local:8765 --tag "#00A4EF"
```

- `--lan` makes the hub listen on `0.0.0.0:8765` (`BUSYBAR_LISTEN`).
  `--hub` writes `BUSYBAR_HUB` on the client: `report.py` posts straight
  to the hub, capped at 1.2 s per hook and backed off for 20 s when the
  hub is unreachable, so an asleep hub never slows Claude Code down.
  Both persist in `env.sh`; a running hub daemon is restarted for you.
- `<hub-name>.local` is the hub's Bonjour/mDNS name (macOS: System
  Settings → General → Sharing → *Local hostname*; Windows 10 1703+
  resolves `.local` natively). If it doesn't resolve on your network,
  use the hub's IP and give it a DHCP reservation in your router.
- `--tag` marks that computer's sessions on the display: a `#RRGGBB`
  color draws a 2×5 flag in the free columns left of the model name
  (costs no text space); one or two letters (`--tag W`) go after the
  model name instead, shortening it if needed (`Fabl 5 max W`).
- `--token SECRET` (same value on hub and clients) makes the hub reject
  LAN reports without it; loopback never needs one. Off by default — the
  hub is meant for a home network. If the hub runs a firewall, allow
  inbound TCP 8765 for Python.
- Codex on a client works the same way: its adapter posts to the hub.

**Which session is shown?** The display follows attention, not chatter.
Among the sessions doing something, the one you last talked to wins —
a prompt you submit, a permission request, or a task starting from idle
pulls the display; tool calls and statusline refreshes never do. When
that session goes idle, whatever is still running surfaces; when
everything is idle, the last one you talked to stays. `GET /status`
includes `host` and `host_tag`; `GET /health` lists every session with
its `focus_ts`.

### When the hub sleeps: a standby

The hub is usually a laptop. Close its lid and the Bar goes dark — unless
a second computer is a **standby**: it runs its own daemon, mirrors its
sessions to the hub while the hub is up, and paints the Bar itself, over
the Bar's own Wi-Fi, the moment the hub is gone. Nothing else changes:
whenever the hub is awake, the hub decides what is shown.

Once, on the computer with the Bar (over USB): put the Bar on your Wi-Fi
(BUSY app → Wi-Fi; `curl http://10.0.4.20/api/wifi/status` shows its LAN
address) and give its Wi-Fi API a key:

```bash
curl -X POST 'http://10.0.4.20/api/access?mode=key&key=1234567890'
```

Then on the standby (Windows: `py setup_claude.py ...`):

```bash
python3 setup_claude.py install --hub http://<hub-name>.local:8765 --standby \
    --transport wifi --device <Bar LAN IP> --device-token 1234567890 --tag "#00A4EF"
```

- The standby takes over after three probes in a row fail (about 10 s) —
  counted only while the Bar itself still answers, so a standby waking
  from its *own* sleep never paints over a live hub — or when the hub
  reports it cannot reach the Bar (unplugged). It hands back the moment
  the hub answers again: resync first, then the hub repaints, then the
  standby stops. `GET http://127.0.0.1:8765/standby` on it shows what it
  thinks; `GET /hub` on either daemon shows role, style and device health.
- Sessions are mirrored as ages, not timestamps (the two clocks may
  disagree by seconds), `state` only when it changed (so even a hub
  running an older daemon arbitrates as if the hooks had reached it
  directly; the lease and `/redraw` need the current one), under a 90 s
  lease refreshed every 30 s — a standby that vanishes takes its sessions
  with it. A hub restart, or a hub that forgot a session while asleep, is
  noticed and resynced within seconds.
- Keep `--style` the same on both computers (the standby logs a warning
  if not) and give the Bar and the hub DHCP reservations. The key grants
  full control of the Bar to anyone on your Wi-Fi: use 10 digits, keep
  the Bar on a trusted network, rotate it over USB if a computer is lost.
  `--no-standby` turns a standby back into a plain forwarder.
- `install` ends with two probes — the hub, and the Bar with the key just
  written — so a wrong key or a closed port is caught right there.

## On-device apps (firmware ≥ 1.2.0)

- `device_app/` + `install_app.py` provide **Claude Status**, an alternative
  JS renderer for the agent dashboard.
- `astra_device_app/` + `install_astra_app.py` provide the standalone
  **Astra Watch** entry in APPS. It requests a fresh non-inference catalog
  check when opened. With `BUSYBAR_X_PULSE=1`, it also runs broad, focused
  access-report, hands-on, and media searches plus an official OpenAI/employee search.
  The default
  `BUSYBAR_X_PULSE_BACKEND=bird` uses a local browser-cookie session through
  the pinned Bird CLI and does not call the paid X API. Set
  `BUSYBAR_X_PULSE_SSH_HOST=local` to run Bird beside the daemon, or an SSH
  host to keep the session on another Mac. `xurl` remains available only via
  the explicit `BUSYBAR_X_PULSE_BACKEND=xurl`; there is no automatic paid
  fallback.
  The bar color still comes only from the newest applicable `@OpenAI` wording.
  Its fill and the `SEED` / `EARLY` / `GROWING` / `BROAD` / `WIDE` label use a
  coarse evidence stage. ChatGPT, API, early-enterprise and ambiguous reports
  are classified separately and never enter the Codex denominator. `R1/W1`
  means one ready and one waiting Codex reporter; once the persistent waiting
  panel observes transitions, `+3/12H` means three `WAITING -> READY` reports
  in 12 hours. The stage is monotone unless an explicit rollback path is added;
  an X complaint spike cannot move it backwards. X-only, unvalidated evidence
  is capped at `EARLY` and quality `Q:L`; only an official all-users statement
  can currently grant `WIDE`.

  Each Bird poll merges broad discovery, focused access-report, hands-on, and
  media searches, then deduplicates stable IDs and near-copied
  launch posts locally. The focused retrieval prevents X's relevance ranking
  from hiding short first-person access reports. `/hub` also exposes 6h/12h
  ready and waiting counts, newest-ready age, fixed-panel size, signal activity,
  classification yield, copy suppression, plan coverage, and auditable stage
  promotion reasons. The explicit `xurl` backend instead uses a saved
  `since_id`. Both retain a 30-day observation and fixed-panel history in
  `BUSYBAR_X_PULSE_STATE` (default
  `~/.local/state/astra-watch/x-pulse.json`). Raw post text and author IDs are
  not persisted. The reporter share and its Wilson interval remain available
  in `/hub` as diagnostics explicitly scoped to classified reporters, not as a
  population estimate. X is checked every six hours by default. Bird uses
  undocumented web GraphQL and may be rate-limited or broken by X changes;
  use it read-only with a non-critical account. When `xurl` is selected,
  OAuth tokens remain on the SSH host and reads are pay-per-use. `READY` gets the
  high-energy rainbow animation. The display refreshes every 2 seconds;
  press `OK` while it is open to refresh both the catalog and X pulse.

  Set `BUSYBAR_X_PULSE_LLM=1` to send only new, high-priority ambiguous posts
  through one cached Codex batch. The default `gpt-5.6-luna` at low effort can
  be replaced with `BUSYBAR_X_PULSE_LLM_MODEL=gpt-5.6-terra`; there is no
  automatic second-model fallback. The classifier runs ephemerally with user
  config, rules, shell, browser, apps, computer use, image generation, and
  multi-agent tools disabled. A strict output schema and a 0.95 acceptance
  gate are enforced; errors fall back to deterministic rules. Reviewed text
  hashes are cached, while raw posts remain unpersisted. A real Luna batch has
  roughly 10k tokens of Codex harness overhead, so batching/caching matter.
  When LLM assistance has reviewed the current window, the bottom row becomes
  `AI<n> COLD/WARM/HOT/FIRE · Rn/Wn`, where `<n>` is the number of recent
  candidates reviewed by the model. Temperature is an ordinal 12-hour momentum
  signal; the top bar remains the separate rollout-evidence stage, colored by
  the current temperature.

In `auto` render mode the daemon observes the hardware selector through the
local status WebSocket. It releases its agent canvas while APPS or SETTINGS is
selected, then restores it on return to CUSTOM/BUSY, so native menus and the
Astra app are never covered by keepalive redraws.

## Firmware field notes (1.1.1)

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

## Repo layout

| File | Purpose |
| --- | --- |
| `daemon.py` | session store + `/status` + device renderer (stdlib only) |
| `x_pulse.py` | bounded X Recent Search over SSH + explicit-report classifier/cache |
| `ai_status.py` | network-only AIWatch monitor + high-priority provider outage overlay |
| `report.py` / `report.sh` | statusline/hook forwarder; auto-spawns the daemon, or forwards to a LAN hub when `BUSYBAR_HUB` is set (unless `BUSYBAR_STANDBY`) (`.py` = cross-platform, `.sh` = POSIX legacy) |
| `setup_claude.py` | wire into / out of `~/.claude` (with backups); `--lan` / `--hub` / `--standby` / `--tag` / `--token` / `--style` for several computers |
| `animgen.py` | `.anim` (bicycle0) encoder + agent-state and AI-alert contours |
| `claude_card.py` | bind the CUSTOM key to the claude theme (and restore) |
| `install_app.py`, `device_app/` | optional on-device Claude Status JS renderer |
| `install_astra_app.py`, `astra_device_app/` | standalone Astra rollout monitor in the BUSY Bar APPS menu |
| `screenshot.py` | grab either the front or back display as an upscaled PNG |
| `docs/EXTENDING.md` | reporting protocol v1, adapter guide, transport guide (incl. BLE design) |
| `adapters/codex_status.py` | Codex adapter (model/effort/speed, context %, quotas — all derived, no name tables) |
| `codex_usage.py` | Account-level quota polling with bounded freshness and reset-aware refreshes |
| `codex_cli.py` | CLI launcher and local app-server bridge for live effort changes |
| `install_codex_cli.py` | Reversible automatic connection for ordinary interactive `codex` commands |
| `codex_target.py` | Foreground Desktop/terminal selection shared by the adapter and dial |
| `adapters/install_codex_autostart.py` | hook the adapter into Codex's `notify` so it auto-starts on use |
| `install_theme.py` | install the on-device "claude" theme (ring + typing companion) |

## Disclaimers

Not affiliated with BUSY or Anthropic. Tested on BUSY Bar firmware
1.1.1 with Claude Code 2.x: the daemon on macOS (Bar on USB), plus a
Windows machine as a hub client over Wi-Fi. The standby role was
verified on the Mac with a second daemon (`daemon.py --port 8766`)
driving the Bar over Wi-Fi while the hub was frozen (`SIGSTOP`) — not yet
from a real Windows standby. The firmware quirks above may change in any
update. MIT licensed.
