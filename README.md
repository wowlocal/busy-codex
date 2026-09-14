# BUSY Codex

**Your Codex session, on a [BUSY Bar](https://busy.app/).**

See what your agent is doing and how much weekly allowance is left. Turn the
dial to change reasoning effort. Press **START** to toggle Fast mode — with
animations that get more intense as effort rises.

![BUSY Codex: effort and Fast mode animations on the 72×16 display](gallery/busy-codex/preview.gif)

[Quick start](#quick-start) · [Controls](#controls) · [Compatibility](#compatibility) ·
[App gallery](https://maxswinkels.github.io/busybar-apps/) · [Documentation](#documentation)

## Controls

| Control | What happens |
| --- | --- |
| **Turn the dial clockwise** | Increase reasoning effort by one supported level. |
| **Turn the dial counterclockwise** | Decrease effort. The lowest and highest levels do not wrap. |
| **Press the large START button** | Toggle Fast mode. Holding the button does not repeat the toggle. |

The controls follow the **foreground app**: the open task in Codex Desktop,
or the focused terminal running our Codex CLI fork. Switch tasks and the
controls follow you. When the target is ambiguous, writes pause.
Hiding an app with **⌘H** keeps the last selected task and its confirmed
settings on the display. Switching between Desktop and CLI updates the
dashboard without briefly returning to the Bar's menu.

Click the **Crown** to open the model picker, rotate to browse, and click again
to apply the selection and return to effort control. Browsing sends no settings
requests. After 12 seconds without input, the picker closes without applying.
Back also cancels, but the firmware may exit the app at the same time.
CLI model selection requires a fork build with the native `model/set` endpoint.

The picker shows a live model card at 25 fps: mint orbits for Luna, blue orbital
planes for Terra, a violet braid for Sol, and a pink stellar core for Astra.
Spark has an amber orbit; GPT-5.5 uses periwinkle. Four small marks show the
model’s visual class; unknown models use a neutral halo with no class assigned.
`ACTIVE` marks the current model, `CLICK` invites confirmation, and the bottom
line counts down to cancellation. `SAVING` becomes `SELECTED` only after
Codex acknowledges the change. Confirmation lasts 1.3 seconds. START pauses
while browsing or saving. The dashboard label keeps the same model color
after the picker closes.

These classes are editable design metadata, not benchmark scores or reasoning
effort. Long names page through in full. See the [model picker preview](docs/img/model-picker.gif).

Changes apply to subsequent turns and preserve Plan mode. They leave a running answer alone and do not change global defaults.
The label appears after Codex confirms the setting.

**Effort has a different feel at each level:** calm flows at lower settings,
blue currents at high, a violet helix at xhigh, gold shockwaves at max and
plasma with sparks at ultra. Only levels supported by the selected model are
offered. Fast ignites a gold warp; standard speed settles into blue rings.

Effort and Fast use 12-pixel bold lettering and native 25 fps effects, then fade back
to the dashboard. See the [effort comparison](docs/img/effort-levels.gif) and
[Fast / standard animation pair](docs/img/fast-modes.gif).

## On the display

| Element | Meaning |
| --- | --- |
| **Model + effort** | The selected session's current model and reasoning level. |
| **Large W bar** | Weekly allowance **remaining**. It shrinks as usage increases. |
| **Small upper bar** | Time elapsed toward the account's next weekly reset. |
| **Animated border + state** | Thinking, working, waiting, done, error or idle. Fast uses a gold working border. |

`DONE` animates for **5 seconds** after completion, then becomes `IDLE`.
Routine status and quota refreshes do not restart the animation.

Weekly usage comes from the signed-in Codex account and refreshes every minute,
including while the task is idle. Reset progress follows the account's actual
quota window. It is neither calendar-week progress nor context usage. Missing
or expired data shows **?**, never an invented full allowance.

## Quick start

You need **Python 3.9+**, a BUSY Bar connected over USB or Wi-Fi, and Codex
signed in with your ChatGPT account. Foreground Desktop and terminal control
currently runs on **macOS**. The live app uses only Python's standard library;
there are no pip dependencies to install.

Clone the repository and run the installer:

```sh
git clone https://github.com/wowlocal/busy-codex.git
cd busy-codex
./install.sh
```

**Install or restart your current checkout:** `./install.sh`.
**Get the latest version and update:** `./install.sh --update`.

The script uses system Python on macOS, builds and verifies the complete app,
uploads its animations, replaces the previous BUSY workers, and runs in the
background. It records logs, status and screenshots under
`~/.local/state/busy-codex/`. You can close the installation terminal. It adds
no login items and does not change or restart Codex itself.

`--update` uses `git pull --ff-only` and refuses uncommitted changes. It never
resets your branch or discards local work. Run the same command after each
update; there is no separate manual build or animation-upload step.

For a new Wi-Fi installation:

```sh
./install.sh --host 192.168.1.50
```

If authentication is enabled, export `BUSYBAR_TOKEN` before running the script.
The script uses the existing token without printing or saving it. Standalone
host, report port and status-only mode are remembered for later updates.

| Installer option | Purpose |
| --- | --- |
| `--update` | Pull the latest commits, then install; put this option first. |
| `--host IP[:PORT]` | Select the device for a standalone installation. |
| `--port 18766` | Select the standalone report port; default is 18765. |
| `--no-effort` / `--effort` | Disable or re-enable standalone Codex controls. |

Existing source/CLI installations are updated in place, preserving `env.sh`,
their report port (usually **8765**), asset namespace and existing supervisor.
The installer pauses drawing while uploading so the CLI watchdog cannot restart
the renderer in the middle of an update. Configure these installations through
`env.sh`; device/port overrides above are for standalone installations.

After installation, open a sent Codex task, switch the Bar to **CUSTOM**, then
click Crown. The script reports unavailable controls or unknown device mode;
a running process alone does not prove the dashboard is visible. See the
[installation guide](docs/INSTALLATION.md) for logs, stopping, and troubleshooting.

### Manual foreground launch

For development, or a complete folder downloaded from the
[gallery](https://maxswinkels.github.io/busybar-apps/), the foreground launcher
remains available. Stop the existing instance first:

```sh
/usr/bin/python3 scripts/build_gallery.py --output "$HOME/.local/share/busy-codex"
/usr/bin/python3 "$HOME/.local/share/busy-codex/app.py"
```

Ctrl-C stops its two workers. `--demo` previews effort/Fast animations;
`--no-upload` is only for assets already uploaded successfully from this build.
The standalone launcher uses port **18765** unless passed `--port`.

## Compatibility

| Codex installation | Account limits | Effort dial | START → Fast |
| --- | :---: | :---: | :---: |
| **Codex Desktop on macOS** | Yes | Yes | Yes, when the model supports Fast |
| **Stock Codex CLI** | Yes | — | — |
| **Our native-control CLI fork** | Yes | Yes | Yes, with `fast/set` support |

For CLI controls, use the
[native-control fork](https://github.com/wowlocal/codex/tree/codex/native-tui-control).
Its launcher enables the local control endpoint; with a raw binary, launch it
with `CODEX_TUI_CONTROL=1 codex`. **Restart existing CLI sessions after updating.**
The earlier `v0.153.4-fork.1-native-control` release supports effort only;
START needs a build that includes `fast/set`.

In a new Desktop chat, send the first message before using the controls: the
app does not expose unsent drafts through its session-control interface.
Desktop control uses a private local IPC interface, so compatibility can change
with Desktop updates. CLI control uses the fork's native, confirmed settings
API. Both require an unambiguous foreground target. See
[setup and troubleshooting](docs/CODEX.md) for terminal focus, multiple windows
and connection diagnostics.

## Local integration

Session state comes from local Codex metadata. Account limits are read through
the installed Codex executable, using its existing login. No prompts are sent
and no model inference or usage-reset credit is needed for monitoring.

Fast mode has the same effect on plan usage as enabling it inside Codex.
The app selects the model's advertised Fast tier and explicitly restores
standard routing when you turn it off.

Animation assets are uploaded once per launch and played by the device. The
app sends state changes and keepalives during normal operation; the firmware
renders the animation frames. No Accessibility or Screen Recording permission
is needed for session controls.

## Documentation

| Guide | Contents |
| --- | --- |
| [Codex setup and troubleshooting](docs/CODEX.md) | Native CLI, optional macOS services, quota freshness and control diagnostics. |
| [Gallery package](gallery/busy-codex/README.md) | Portable app folder, emulator preview and gallery checks. |
| [Pixel UI](docs/PIXEL_UI.md) | Animation scenes, bitmap type and PNG/GIF exports. |
| [Extension guide](docs/EXTENDING.md) | Reporting API, adapters and device transports. |
| [Display configuration](docs/CONFIGURATION.md) | Optional avatar style, themes and extra monitors. |
| [Claude Code](docs/CLAUDE_CODE.md) | The original hook integration and shared displays across computers. |
| [Firmware notes](docs/FIRMWARE.md) | Hardware observations and native animation format details. |

The [Chinese documentation](README.zh-CN.md) covers the original Claude Code integration.

## Development

```sh
python3 -m unittest discover -s tests
```

The optional [preview exporter](docs/PIXEL_UI.md) needs Pillow; the app itself
does not. The [gallery guide](gallery/busy-codex/README.md#preview-and-conformance)
explains how to capture synthetic previews and run device API checks.

## Credits and license

Built on [Alpharius-003/busybar-claude-status](https://github.com/Alpharius-003/busybar-claude-status),
with Codex session controls, account usage monitoring and native pixel effects.
Released under the [MIT license](LICENSE). Independent community project;
not affiliated with OpenAI, BUSY or Anthropic.
