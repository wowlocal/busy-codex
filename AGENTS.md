# Working in this repository

## What “start the app” means

This repository is **BUSY Codex**, the Codex dashboard and physical controls
for a BUSY Bar. A request to start the app in this repository means running
that dashboard on the Bar. Do not interpret it as opening Codex Desktop with
`open -a Codex`. Read the latest commits and the launch instructions before
acting; the older Astra AI and Claude device apps are separate integrations.

## Build and launch on this Mac

- Use `/usr/bin/python3` for the live app on this Mac. In the September 11,
  2026 session, pyenv Python 3.11.11 started the workers but foreground
  detection failed with `macOS background registration failed (status=-50)`.
  The same check succeeded with system Python. This is a local runtime
  observation, not a claim that all pyenv installations fail.
- First check for an existing launcher and its workers, and inspect their
  `/hub` endpoint. Run only one instance owning the Bar's controls.
- Build the complete external app folder from the current checkout:

  ```sh
  /usr/bin/python3 scripts/build_gallery.py --output "$HOME/.local/share/busy-codex"
  /usr/bin/python3 "$HOME/.local/share/busy-codex/app.py"
  ```

- `run_app.py` is the source launcher; the runnable package entrypoint is
  the generated `app.py`. Running the source directly without packaged assets
  fails. The builder records the source revision and file hashes in
  `SOURCE.json`; rebuild after source updates.
- The default device is USB `10.0.4.20`, bound through source `10.0.4.21`.
  Use `--host IP[:PORT]` for Wi-Fi and an existing `BUSYBAR_TOKEN` if needed.
  Do not print authentication tokens.
- The first launch uploads the native animations, then starts `daemon.py`
  and `adapters/codex_status.py`. Allow the upload to finish. `--no-upload`
  is appropriate only after this version's assets were uploaded successfully.
- The local report port defaults to `18765`. On restart, send SIGTERM to the
  verified launcher PID and wait for its children to exit. If the port remains
  unavailable, check its owner; `--port 18766` can be used for the replacement
  instance once the old instance has stopped. Use the selected port in checks.
- For a background launch, use `subprocess.Popen` with `start_new_session=True`,
  stdin set to `DEVNULL`, and stdout/stderr redirected to a known log file
  such as `/tmp/busy-codex.log`. Record the PID and inspect the log afterward.
  Do not install LaunchAgents or login items merely to satisfy a launch request.

## Verify the actual device display

A running process or a successful health check does **not** prove that the
dashboard is visible. Check all of the following before reporting success:

```sh
curl --noproxy '*' -s http://127.0.0.1:18765/status
curl --noproxy '*' -s http://127.0.0.1:18765/hub
/usr/bin/python3 screenshot.py /tmp/busy-codex-front.png
/usr/bin/python3 screenshot.py /tmp/busy-codex-back.png back
```

Inspect the captured PNGs. `/status` reports the selected session and quotas;
`/hub` reports `device_mode`, `rendering`, `device_error`, input connection,
and `codex_target`/`codex_effort` diagnostics. Wait for device input to connect
and report a mode: initial `device_mode: null` and `rendering: true` alone
are not sufficient evidence of a visible, usable dashboard.

- **APPS and SETTINGS suppress the dashboard** in `device_canvas_allowed()`.
  The “Astra AI / Start / Setup” screen seen in this session was a device
  menu, not BUSY Codex. Ask the user to exit that menu (Back as appropriate)
  and switch out of APPS/SETTINGS, then verify the reported mode and screenshot.
  Do not claim visibility before confirming it.
- The standalone launcher does **not** install an entry in the Bar's APPS
  menu. Its dashboard is drawn by the Mac workers through the device API.
- An active focus session or another application can own the display;
  HTTP 409 means the draw was refused. Diagnose before changing device state.
- Controls follow a sent task in foreground Codex Desktop or an unambiguous
  focused CLI session with native TUI control. An unrelated foreground app
  or an unsupported CLI can block controls even while status data is available.
  Read `codex_target.error` and the last input's `reason` instead of guessing.
- If USB initially reports `No route to host`, check the USB interface and
  retry once it is available. An exited launcher is not a successful launch.

See `README.md`, `gallery/busy-codex/README.md`, and `docs/CODEX.md` for the
full launcher and integration details.
