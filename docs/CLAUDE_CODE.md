# Claude Code and shared displays

[Back to BUSY Codex](../README.md) · [中文文档](../README.zh-CN.md)

This is the optional Claude Code integration inherited from the original project.
It uses statusline and lifecycle hooks, with a shared display daemon.
For Codex, use the [standalone app](../README.md#quick-start); Claude hooks are not required.

## Display styles

**Avatar** — the Clawd companion acts out the session state.

![Claude Code avatar](img/avatar-working.png)

**Minimal** — model, quotas and state stay visible.

![Claude Code minimal display](img/working.png)

Select a style in `env.sh` with `export BUSYBAR_STYLE=avatar` or `minimal`.
See [display configuration](CONFIGURATION.md#display-styles) for behavior and themes.

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
git clone https://github.com/wowlocal/busy-codex.git
cd busy-codex
```

Upload the base animations using the [manual asset setup](CONFIGURATION.md#manual-animation-upload),
then connect the hooks:

```sh
python3 setup_claude.py install
```

The installer backs up the existing configuration. Start a Claude Code session;
the daemon auto-spawns on the first statusline refresh and the display appears.
`setup_claude.py uninstall` reverses everything.

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
