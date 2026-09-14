# Install and update BUSY Codex

[Back to README](../README.md)

## One command

From this checkout, run:

```sh
./install.sh
```

Use the same script to download and install the latest upstream version:

```sh
./install.sh --update
```

The update option must come first. It runs `git pull --ff-only`, then reopens
the updated installer. Uncommitted changes stop the update before fetching or
changing files. Commit or stash your work, or omit `--update` to install the
current checkout. Divergent branches are never reset or force-merged.

Requirements: Python 3.9+, Git for source updates, and a USB or Wi-Fi BUSY Bar.
The script supports macOS/Linux; foreground Codex control is macOS-specific.
On macOS it selects `/usr/bin/python3` for reliable foreground detection. It
uses the standard library, with no pip installation, sudo or login items.

## What happens

1. Identify this checkout’s and the installed package’s BUSY workers. The
   installer never stops a Codex CLI session or an unrelated port owner.
2. Build in a temporary directory and verify every file against `SOURCE.json`.
   Check device connectivity before interrupting the old installation.
3. Stop the verified previous BUSY workers. Preserve files you added to the
   install folder and copy the complete build into `~/.local/share/busy-codex`.
4. Upload all native animations before normal rendering resumes.
5. Start the app and collect `/status`, `/hub` and both device screenshots.

For a fresh installation, the packaged `app.py` runs detached on port **18765**.
You can close the terminal. Updates remember its device, port and status-only
setting in `~/.local/state/busy-codex/install.json`. Only run one installation
for a Bar. The installer serializes concurrent invocations using its state lock.

The older `install_app.py` and `install_astra_app.py` scripts install separate
device integrations. They are not the BUSY Codex installer. BUSY Codex is drawn
by Mac workers and does not add an entry to the Bar’s APPS menu.

## Existing CLI or source services

When the existing workers run from this checkout, the installer preserves
that arrangement: the same `env.sh`, report port (normally **8765**) and asset
namespace. It builds a verified package too, but the live workers continue to
use the checkout. Keep the checkout in place.

A legacy CLI watchdog may restart workers automatically. Rather than starting
a second packaged instance, the installer restarts the source workers and uses
a loopback-only maintenance lease to release the canvas during upload. The
health endpoint stays available, so the watchdog sees a healthy worker. The
lease is renewed during uploads, cleared afterward (including on errors), and
expires after 60 seconds if the installer crashes. Existing adapter startup
locking prevents duplicates. No new LaunchAgents are installed.

If both source and standalone installations are already running, the script
stops with an explanation so you can identify the duplicate. It does not kill
unknown processes. For an existing source installation, edit device/port/effort
settings in `env.sh`, then run `./install.sh` without standalone overrides.

## Wi-Fi and options

```sh
./install.sh --host 192.168.1.50
./install.sh --update --host 192.168.1.50 --port 18766
./install.sh --no-effort
```

For authenticated devices, export your existing `BUSYBAR_TOKEN`. Tokens are
passed to the device but never printed or written to installation metadata.
USB defaults to `10.0.4.20`, bound through source address `10.0.4.21`.

`--no-effort` is remembered for standalone updates. To re-enable controls,
run `./install.sh --effort`.
Advanced paths can be selected with `--install-dir` (an external directory
named `busy-codex`) and `--state-dir`; keep using the same paths on updates.

## Verify and troubleshoot

The installer prints the selected report URL. Its state directory contains:

- `app.log`: output from the workers it starts.
- `install.json`: mode, device, report port, build revision and standalone PID.
- `hub.json` and `status.json`: diagnostics captured after startup.
- `front.png` and `back.png`: snapshots of the actual device displays.

Legacy watchdog-started workers may also log to `~/.claude/busybar-daemon.log`
and `~/.claude/busybar-codex-adapter.log`.

Open a sent task in foreground Codex Desktop or the supported native CLI,
move the Bar switch to **CUSTOM**, then press Crown. APPS/SETTINGS suppress the
dashboard. If mode is unknown, move the switch to another position and back.
A connected input stream and a running process are not proof of visibility;
inspect the screenshots and confirm the physical controls.

Failed device authentication or an offline device stops installation. An
upload/startup failure after the old worker stopped can leave the display
unavailable; correct the reported problem and rerun `./install.sh`. The script
does not claim success or silently skip failed assets. Local source edits are
not rolled back. It does not change a focus session or steal another app’s
canvas to overcome an HTTP 409.

For a current snapshot (use the port printed by the installer):

```sh
curl --noproxy '*' -s http://127.0.0.1:18765/hub
curl --noproxy '*' -s http://127.0.0.1:18765/status
```

To stop a standalone installation, read `launcher_pid` in `install.json`, verify
that PID still runs this install folder’s `app.py` with `ps -p PID -o command=`,
then run `kill -TERM PID`. The launcher stops its workers and releases its own
canvas. A source installation can be restarted by a legacy CLI watchdog or
notification hook; see [Codex setup](CODEX.md#optional-macos-background-services)
for the existing service and hook configuration.
