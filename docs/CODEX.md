# Codex setup and troubleshooting

[Back to BUSY Codex](../README.md)

Start with the [standalone app](../README.md#quick-start). This guide covers
native CLI controls, optional background services and diagnostics. Commands
below run from the source checkout unless stated otherwise.

## Codex Desktop

Run BUSY Codex on the same Mac as Codex Desktop. The adapter follows task-view
mount and unmount events in `~/Library/Logs/com.openai.codex`; background model
output cannot select a task. The controller changes settings through the app's
private, versioned IPC interface (snapshot v11, settings request v1, verified
against the September 2026 app).

Keep tasks in **one primary Codex window**. Multiple visible primary windows,
Home/Settings pages, missing view events or a disconnected app pause writes.
The app does not require Accessibility, Screen Recording, UI automation or
changes to Codex Desktop's files.

## Native CLI control

Use the [native-control fork](https://github.com/wowlocal/codex/tree/codex/native-tui-control)
with `effort/set` and `fast/set`. The earlier
`v0.153.4-fork.1-native-control` release supports effort only. **Restart existing
CLI sessions after updating**: running processes retain their loaded binary.

The fork launcher enables the endpoint. To opt in with a raw binary:

```sh
CODEX_TUI_CONTROL=1 codex
CODEX_TUI_CONTROL=1 codex resume <session-id>
```

`CODEX_TUI_CONTROL=0` disables it. The same-user endpoint lives in a private
`$CODEX_HOME/tui-control/<instance>` directory and exposes settings and status.
The TUI publishes its selected task, focus, model, effective effort, service
tier and supported controls. BUSY Codex follows the native acknowledgement by
request ID. A dropped connection is recovered by reading that request's outcome
without repeating the write.

Foreground app selection is macOS-specific. Ghostty, Terminal, iTerm2, WezTerm,
Kitty, Alacritty and Warp are recognized. Terminal tabs and multiplexers must
forward focus reports. If several sessions claim focus, control pauses until
the target is unambiguous. Switching to an unrelated app disables writes while
keeping the usage display available.

When an app is hidden or focus reports are still arriving, the display keeps
the last selected task. Its last confirmed model, effort and Fast setting stay
visible; they do not authorize writes while the task is unfocused. A replacement
session is published before the previous report is retired, so a Desktop/CLI
handoff does not leave an empty display.

When migrating from the old command shim, run
`python3 install_codex_cli.py uninstall` to restore the original command, then
point `codex` at the updated fork launcher. The legacy bridge remains only for
older running sessions; native launches bypass the Python PTY/WebSocket proxy
and terminal-title parsing. Custom terminal titles are unrestricted.

See [protocol design and upstream discussions](NATIVE_CLI_CONTROL_PROPOSAL.md)
and [integration research](CODEX_INTEGRATION_RESEARCH.md). An opt-in check starts
an isolated TUI using a temporary copy of the existing login and model cache,
without submitting model prompts:

```sh
python3 tests/check_codex_cli_native_live.py /path/to/native/codex-or-launcher
```

## Settings and feedback

Clockwise raises effort; counterclockwise lowers it. Available levels come from
the selected model's catalog, not a fixed list. The controller keeps a confirmed
Desktop catalog entry for up to five minutes if the shared cache becomes
unreadable; a fresh valid entry always takes precedence.

START toggles the model's advertised Fast service tier. Turning it off sends
explicit standard routing, including for models whose default tier is Fast.
Both controls preserve the current model and Plan mode, apply to subsequent
turns and do not rewrite global defaults.

The first dial detent wakes the controller immediately. Rapid turns are combined
during a pending request, with at most one confirmed update per 40 ms. Feedback
appears only after confirmation, and its device submission takes priority over
dashboard updates. Bold lettering becomes fully visible in 80 ms; another
change replaces it without replaying the entrance. The overlay fades out after
1.8 seconds. Native playback is 25 fps.

[Effort animation comparison](img/effort-levels.gif) ·
[Fast and standard-speed animations](img/fast-modes.gif) ·
[Pixel renderer](PIXEL_UI.md)

Menus and optional Astra Watch/provider outage overlays retain their own
controls. When one of those screens owns the display, START and the dial do
not change Codex settings.

## Account limits

The adapter reads `account/rateLimits/read` through a short-lived local Codex
app-server every 60 seconds, even while the selected task is idle. It uses the
account's `codex` bucket in `rateLimitsByLimitId`. Windows are identified by
duration, so a weekly window can be primary or secondary; a lone five-hour
window never becomes a week. Historical task usage cannot replace this data.

The large **W** bar is `100 − usedPercent`. The small upper bar is elapsed time
toward the account's actual weekly reset. It measures the quota window, not the
calendar week or context usage. A reset triggers an early refresh.

On temporary failures, the last confirmed snapshot can remain usable for up to
three minutes, but never past its reset. Expired data clears both fills and
shows **?** until fresh data arrives. It never implies a replenished allowance.

The installed Codex executable handles authentication through its existing
ChatGPT login. The Desktop-bundled executable is preferred on macOS, then
`codex` on PATH. Each poll initializes the app-server, reads limits and closes
it; it does not start a task, call a model or consume a reset credit.

## Optional macOS background services

This is an alternative to keeping the standalone launcher open. Stop that app
first. The services run from the source checkout, use port **8765**, and load
`env.sh` beside `daemon.py`. The standalone app uses port **18765**, its own
canvas and launch options, and does not read `env.sh`.

Follow [manual animation upload](CONFIGURATION.md#manual-animation-upload) to
prepare the base assets, then install the effort/Fast assets and services:

```sh
python3 install_effort_anims.py
python3 native_services.py install
```

The daemon and adapter start at login as background processes, without Python
Dock icons. `KeepAlive` is disabled, so launchd does not repeatedly restart a
process you quit. Existing Codex notification hooks can still start them on
Codex use; remove that optional hook with
`python3 adapters/install_codex_autostart.py uninstall` if desired.

Run the install command again to restart both services after a source update.
To remove the service registration, unload and delete its two LaunchAgents:

```sh
launchctl bootout "gui/$(id -u)/local.busy-codex.daemon"
launchctl bootout "gui/$(id -u)/local.busy-codex.adapter"
rm "$HOME/Library/LaunchAgents/local.busy-codex.daemon.plist"
rm "$HOME/Library/LaunchAgents/local.busy-codex.adapter.plist"
```

## Configuration

| Variable | Purpose |
| --- | --- |
| `CODEX_HOME` | Select another Codex data directory. |
| `BUSYBAR_CODEX_BIN` | Override the executable used to read account limits. |
| `BUSYBAR_CODEX_LIMIT_ID` | Intentionally select an account bucket other than `codex`. |
| `BUSYBAR_CODEX_LOG_DIR` | Override the Desktop log directory. |
| `BUSYBAR_CODEX_IPC` | Override the Desktop IPC socket path. |
| `BUSYBAR_CODEX_THREAD_ID` | Pin a task in the service setup; the standalone launcher clears this override. |
| `BUSYBAR_CODEX_EFFORT=0` | Disable both controls in the service setup; use `--no-effort` with the standalone app. |

Export variables before launching the standalone app. For the services, add
`export NAME=value` lines to `env.sh` and restart. If launching the source daemon
and adapter manually, source that file before starting both processes.

## Diagnostics

Inspect the standalone app's local status:

```sh
curl --noproxy '*' -s http://127.0.0.1:18765/hub | python3 -m json.tool
curl --noproxy '*' -s http://127.0.0.1:18765/status | python3 -m json.tool
```

Use port **8765** for the service installation, or your custom `--port`.

| Symptom | What to check |
| --- | --- |
| Nothing happens on START or rotation | In `/hub`, inspect `device_input.last_start` or `last_encoder` for the event and blocking reason. Check that the Codex dashboard owns the display. |
| Wrong or missing target | `codex_target` shows Desktop/CLI selection and `foreground_bundle`; `codex_focus` shows Desktop view evidence. Keep one primary Desktop window or one focused native CLI session. |
| CLI control reports an error | Restart with the updated fork, verify native control is enabled and inspect `codex_effort.error`. Fast needs `fast/set`. |
| A change feels slow | `codex_effort.confirmation_ms` measures time to acknowledgement; `display_ms` includes successful device submission. Neither includes the matrix's frame interval. |
| Speed looks wrong | `codex_effort.fast` and `service_tier` are the current confirmed values. |
| Weekly gauges show `?` | In `/status`, inspect `quota_status`, `observed_at`, `valid_until` and the quota window duration/reset. Check the installed Codex login. |
| No device connection | Inspect device health in `/hub`, USB/Wi-Fi connectivity, the host address and `BUSYBAR_TOKEN` for an authenticated Wi-Fi API. |

For a one-off quota refresh, use
`python3 adapters/codex_status.py --once -v`. Set `BUSYBAR_PORT=18765` when
reporting to the standalone app. Notification hooks use `--no-usage-refresh`
so frequent turns do not multiply account requests.

Foreground runs print logs to the terminal. The service logs are
`~/.claude/busybar-daemon.log` and `~/.claude/busybar-codex-adapter.log`
(the directory name is retained from the original integration).
