# Display configuration and optional integrations

[Back to BUSY Codex](../README.md)

These options apply to the source daemon and service installation. Set them
in an `env.sh` beside `daemon.py` and restart the services after editing.
The standalone gallery launcher deliberately selects the minimal Codex display
and disables the optional monitors; it does not load `env.sh`.

## Manual animation upload

The standalone app uploads its own assets automatically. For a source daemon
or service installation, run this from the checkout to upload the base rings
and optional monitor assets. Stop the renderer first: firmware cannot replace
an animation while it is playing. The transport uses the same device and token
configuration as the daemon.

```sh
python3 - <<'PY'
import animgen
import daemon
import urllib.request

transport = daemon.make_transport()
for app, animations in (
    (daemon.APP_NAME, animgen.ANIMS),
    ("ai_provider_status", animgen.AI_STATUS_ANIMS),
    ("astra_watch_ai", animgen.ASTRA_STATUS_ANIMS),
):
    for name, (generate, width, height, fps) in animations.items():
        frames = generate()
        blob = animgen.encode_anim(frames, fps=fps, w=width, h=height)
        animgen.decode_check(blob, frames, w=width, h=height)
        request = urllib.request.Request(
            transport.base + f"/assets/upload?application_name={app}&file={name}",
            data=blob, method="POST", headers=transport.headers)
        with transport.opener.open(request, timeout=20) as response:
            response.read()
        print("uploaded", app, name)
PY
```

For Codex controls, also run `python3 install_effort_anims.py` before starting
the [services](CODEX.md#optional-macos-background-services).

## Display styles

Two looks, one codebase — pick with `BUSYBAR_STYLE` (persist it in an
`env.sh` next to `daemon.py`, e.g. `export BUSYBAR_STYLE=avatar`):

- **`minimal`** (default) — the standard layout: state word + weekly gauges always
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

  ![Claude theme](img/claude-theme.png)

  Picking it toggles the display on, picking another theme toggles it
  off. (In `auto` mode the theme is unrelated to the status display —
  it's just a theme.) `python3 claude_card.py install` binds the physical
  CUSTOM key to it (backs up your current card; `restore` undoes).
- **`off`** — data bridge only (`GET /status` on `127.0.0.1:8765` and the
  USB interface for the future on-device app).

## Astra rollout indicator

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

## Provider outage overlay

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

## On-device apps

Requires BUSY Bar firmware 1.2.0 or later.

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
  the explicit `BUSYBAR_X_PULSE_BACKEND=xurl`, with your own
  `BUSYBAR_X_PULSE_APP` and `BUSYBAR_X_PULSE_USERNAME`; there is no automatic paid
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
