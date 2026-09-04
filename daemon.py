#!/usr/bin/env python3
"""BusyBar agent-status daemon.

A provider-agnostic status display core with pluggable adapters and
transports:

    claude-code (built-in adapter: /statusline + /state) --.
    codex / cursor / anything (POST /v1/report) ----------+--> SessionStore
                                                          |       |
                              GET /status  <--------------+   Renderer
                              (device JS app, debugging)          |
                                                              Transport
                                                        (usb / wifi / cloud)

The CORE understands only the normalized report schema (see
docs/EXTENDING.md): state, label, label_color, context_pct, quotas.
Everything Claude-specific — statusline JSON parsing, /effort palette
colors, the 5h/7d rate-limit windows — lives in the claude adapter
functions and never leaks into the renderer.

Layout (72x16 front display):

    ############################    1px per-pixel animated ring (.anim)
    #  Fable 5 max      [##----] #  label            | time-to-reset progress
    #  W [quota bar] A     WORK   #  quota remaining | Astra | state word
    ############################
"""

from __future__ import annotations

import hmac
import http.client
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ai_status
import x_pulse

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

# `daemon.py --port N` exists for test rigs only (a second daemon on the same
# computer); report.py/report.sh always talk to 8765.
LISTEN_PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
# Bind addresses (env BUSYBAR_LISTEN, comma-separated). Default: loopback +
# the USB network (device side) only. "0.0.0.0" turns this daemon into the
# hub for the other computers on your LAN (README: "Several computers").
LISTEN_ADDRS = [a.strip() for a in
                os.environ.get("BUSYBAR_LISTEN", "127.0.0.1,10.0.4.21").split(",")
                if a.strip()] or ["127.0.0.1"]
# Optional shared secret for reports arriving from other machines (env
# BUSYBAR_HUB_TOKEN on the hub and on every client). Loopback never needs it.
HUB_TOKEN = os.environ.get("BUSYBAR_HUB_TOKEN", "")
# Standby role (README: "When the hub sleeps"): BUSYBAR_HUB names the hub;
# with BUSYBAR_STANDBY=1 this daemon mirrors every session there and
# renders only while the hub has been unreachable for HUB_DOWN_AFTER_S.
HUB_URL = os.environ.get("BUSYBAR_HUB", "").strip().rstrip("/")
STANDBY = os.environ.get("BUSYBAR_STANDBY", "").strip().lower() in ("1", "true", "yes", "on")
if STANDBY and not HUB_URL:
    sys.exit("BUSYBAR_STANDBY needs BUSYBAR_HUB (the hub's URL)")
HUB_POLL_S = 3.0            # standby: probe cadence while nothing is queued
HUB_TIMEOUT_S = 2.0         # ... per request to the hub
HUB_DOWN_FAILURES = 3       # probes failing in a row (~10 s) before a standby takes over
MIRROR_LEASE_S = 90.0       # the hub forgets a mirrored session not refreshed within this
MIRROR_HEARTBEAT_S = 30.0   # ... so a standby re-mirrors everything live this often
SUSPEND_GAP_S = 30.0        # a loop iteration this late means the computer was asleep
INSTANCE = secrets.token_hex(8)   # this process; GET /hub reports it
# LAN and loopback traffic never goes through a proxy (HTTP_PROXY, Windows
# system proxy): a proxy is never the route to 127.0.0.1, the USB link, the
# Bar's LAN address or the hub's .local name.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
USB_SOURCE_IP = os.environ.get("BUSYBAR_USB_SOURCE_IP", "10.0.4.21").strip()

# How the daemon renders to the device (env BUSYBAR_RENDER_MODE overrides):
#   "auto"  - whenever any reporting agent is active (the always-on behavior)
#   "theme" - only while "claude" is the device's currently selected
#             BUSY/CUSTOM theme (on-device manual switch; see claude_card.py)
#   "off"   - data bridge only
RENDER_MODE = os.environ.get("BUSYBAR_RENDER_MODE", "auto")
# Display style (env BUSYBAR_STYLE): "minimal" (text state word + quota
# gauges) or "avatar" (a pixel companion acts out the state).
STYLE = os.environ.get("BUSYBAR_STYLE", "minimal")
AI_STATUS_ENABLED = os.environ.get("BUSYBAR_AI_STATUS", "").strip().lower() \
    in ("1", "true", "yes", "on")
AI_STATUS_URL = os.environ.get("BUSYBAR_AI_STATUS_URL", ai_status.API_URL).strip()
X_STATUS_URL = os.environ.get("BUSYBAR_X_STATUS_URL", ai_status.X_STATUS_URL).strip()
GOOGLE_STATUS_URL = os.environ.get(
    "BUSYBAR_GOOGLE_STATUS_URL", ai_status.GOOGLE_STATUS_URL,
).strip()
AI_STATUS_POLL_S = float(os.environ.get("BUSYBAR_AI_STATUS_POLL_S", ai_status.POLL_S))
ASTRA_STATE_PATH = os.path.expanduser(os.environ.get(
    "BUSYBAR_ASTRA_STATE",
    "~/.local/state/astra-watch/state.json",
))
ASTRA_STALE_S = float(os.environ.get("BUSYBAR_ASTRA_STALE_S", "1800"))
ASTRA_CHECK_INTERVAL_S = max(60.0, float(os.environ.get(
    "BUSYBAR_ASTRA_CHECK_INTERVAL_S", "600",
)))
ASTRA_WATCH_SCRIPT = os.path.expanduser(os.environ.get(
    "BUSYBAR_ASTRA_WATCH_SCRIPT",
    "~/plugins/astra-watch/scripts/astra_watch.py",
))
X_PULSE_ENABLED = os.environ.get("BUSYBAR_X_PULSE", "").strip().lower() \
    in ("1", "true", "yes", "on")
X_PULSE_BACKEND = os.environ.get("BUSYBAR_X_PULSE_BACKEND", "bird").strip().lower()
X_PULSE_SSH_HOST = os.environ.get("BUSYBAR_X_PULSE_SSH_HOST", "mija").strip()
X_PULSE_APP = os.environ.get("BUSYBAR_X_PULSE_APP", "mija-x").strip()
X_PULSE_USERNAME = os.environ.get(
    "BUSYBAR_X_PULSE_USERNAME", "MishaNevazhno",
).strip()
X_PULSE_POLL_S = max(300.0, float(os.environ.get(
    "BUSYBAR_X_PULSE_POLL_S", "21600",
)))
X_PULSE_MAX_RESULTS = max(10, min(100, int(os.environ.get(
    "BUSYBAR_X_PULSE_MAX_RESULTS", "25",
))))
X_PULSE_MAX_PAGES = max(1, min(5, int(os.environ.get(
    "BUSYBAR_X_PULSE_MAX_PAGES", "1",
))))
X_PULSE_BIRD_PATH = os.environ.get(
    "BUSYBAR_X_PULSE_BIRD_PATH", "/usr/local/bin/bird",
).strip()
X_PULSE_STATE_PATH = os.path.expanduser(os.environ.get(
    "BUSYBAR_X_PULSE_STATE",
    "~/.local/state/astra-watch/x-pulse.json",
))
X_PULSE_CLASSIFIER_VALIDATED = os.environ.get(
    "BUSYBAR_X_PULSE_CLASSIFIER_VALIDATED", "",
).strip().lower() in ("1", "true", "yes", "on")
X_PULSE_LLM_ENABLED = os.environ.get(
    "BUSYBAR_X_PULSE_LLM", "",
).strip().lower() in ("1", "true", "yes", "on")
X_PULSE_LLM_MODEL = os.environ.get(
    "BUSYBAR_X_PULSE_LLM_MODEL", "gpt-5.6-luna",
).strip()
X_PULSE_LLM_MAX_ITEMS = max(1, min(16, int(os.environ.get(
    "BUSYBAR_X_PULSE_LLM_MAX_ITEMS", "12",
))))
X_PULSE_LLM_TIMEOUT_S = max(20.0, float(os.environ.get(
    "BUSYBAR_X_PULSE_LLM_TIMEOUT_S", "90",
)))
X_PULSE_CODEX_PATH = os.path.expanduser(os.environ.get(
    "BUSYBAR_X_PULSE_CODEX_PATH", "codex",
)).strip()
APP_NAME = "claude_status"   # canvas app name; .anim assets live under it
DRAW_PRIORITY = 50
THEME_NAME = "claude"        # installed in /ext/apps_assets/busy/themes/
SNAPSHOT_POLL_S = 2.0

TEXT_TIMEOUT_S = 15
ANIM_TIMEOUT_S = 120
ANIM_REFRESH_S = 60.0
KEEPALIVE_S = 8.0
COMPLETE_HOLD_S = 30.0
# Idle release: after this many seconds of IDLE the screen is handed
# back to the device (env BUSYBAR_IDLE_CLEAR_S; 0 = keep forever).
IDLE_CLEAR_AFTER_S = float(os.environ.get("BUSYBAR_IDLE_CLEAR_S", "600"))
DEFAULT_TTL_S = 6 * 3600

STATES = ("THINKING", "WORKING", "WAIT", "ERROR", "FAILED", "COMPLETE", "IDLE")
STATE_ANIMS = {
    "THINKING": "think.anim", "WORKING": "work.anim", "WAIT": "wait.anim",
    "ERROR": "error.anim", "FAILED": "error.anim", "COMPLETE": "done.anim",
    "IDLE": "idle.anim",
}
STATE_WORDS = {
    "THINKING": "THINK", "WORKING": "WORK", "WAIT": "WAIT",
    "ERROR": "ERR", "FAIL": "FAIL", "FAILED": "FAIL", "COMPLETE": "DONE",
    "IDLE": "IDLE",
}
STATE_WORDS_FULL = {
    "THINKING": "thinking", "WORKING": "working", "WAIT": "waiting",
    "ERROR": "error", "FAILED": "failed", "COMPLETE": "done", "IDLE": "idle",
}
AVATAR_ANIMS = {
    "THINKING": "claw_think.anim", "WORKING": "claw_work.anim",
    "WAIT": "claw_wait.anim", "ERROR": "claw_error.anim",
    "FAILED": "claw_error.anim", "COMPLETE": "claw_done.anim",
    "IDLE": "claw_idle.anim",
}
AVATAR_X, AVATAR_Y = 55, 1

STATE_COLORS = {
    "THINKING": "#AF87FFFF", "WORKING": "#FFB000FF", "WAIT": "#FF6A00FF",
    "ERROR": "#FF2020FF", "FAILED": "#FF2020FF", "COMPLETE": "#20C040FF",
    "IDLE": "#808080FF",
}

LABEL_FALLBACK_COLOR = "#FFFFFFFF"
HOST_TAG_COLOR = "#8C8C8CFF"   # text host tag (protocol `host_tag`)
# States that pull the display to a session: the user just spoke to it
# (THINKING) or it is asking for the user (WAIT).
ATTENTION_STATES = ("THINKING", "WAIT")
QUOTA_COLOR = "#A0A0A0FF"
FONT = "small"

BAR_X, BAR_Y, BAR_W, BAR_H = 50, 3, 20, 4
BAR_TRACK_COLOR = "#262626FF"
LABEL_MAX_PX = BAR_X - 2 - 3
QUOTA_BAR_X, QUOTA_BAR_Y, QUOTA_BAR_W, QUOTA_BAR_H = 10, 10, 35, 4
WEEK_SECONDS = 7 * 24 * 60 * 60
ASTRA_COLORS = {
    "waiting": "#606060FF", "hidden": "#FFB000FF",
    "available": "#20C040FF", "error": "#FF2020FF",
    "stale": "#FF2020FF", "unknown": "#00000000",
}
ASTRA_REFRESH_LOCK = threading.Lock()
ASTRA_REFRESHED_AT = 0.0
ASTRA_APP_LOCK = threading.Lock()
ASTRA_APP_LAST_SEEN = 0.0
ASTRA_APP_REQUESTS = 0
ASTRA_APP_ACTIVE_S = 5.0


def _fetch_x_pulse() -> dict:
    return x_pulse.fetch(
        X_PULSE_SSH_HOST,
        backend=X_PULSE_BACKEND,
        app=X_PULSE_APP,
        username=X_PULSE_USERNAME,
        max_results=X_PULSE_MAX_RESULTS,
        state_path=X_PULSE_STATE_PATH,
        classifier_validated=X_PULSE_CLASSIFIER_VALIDATED,
        max_pages=X_PULSE_MAX_PAGES,
        bird_path=X_PULSE_BIRD_PATH,
        llm_enabled=X_PULSE_LLM_ENABLED,
        llm_model=X_PULSE_LLM_MODEL,
        llm_max_items=X_PULSE_LLM_MAX_ITEMS,
        llm_timeout_s=X_PULSE_LLM_TIMEOUT_S,
        codex_path=X_PULSE_CODEX_PATH,
    )


X_PULSE_MONITOR = x_pulse.Monitor(
    _fetch_x_pulse,
    interval_s=X_PULSE_POLL_S,
    stale_s=max(3 * X_PULSE_POLL_S, 6 * 3600),
) if X_PULSE_ENABLED else None
if X_PULSE_MONITOR is not None:
    try:
        cached = x_pulse.cached_summary(
            X_PULSE_STATE_PATH,
            backend=X_PULSE_BACKEND,
            classifier_validated=X_PULSE_CLASSIFIER_VALIDATED,
            llm_enabled=X_PULSE_LLM_ENABLED,
            llm_model=X_PULSE_LLM_MODEL,
        )
        if cached is not None:
            X_PULSE_MONITOR.seed(cached[0], checked_at=cached[1])
    except Exception:
        # A damaged cache must never prevent the local status daemon starting.
        pass


# --------------------------------------------------------------------------
# Transports (env BUSYBAR_TRANSPORT: usb | wifi | cloud; see docs/EXTENDING.md)
# --------------------------------------------------------------------------

class HttpTransport:
    """Busy Bar HTTP API over any of its three routes. Remembers whether
    the device answers (device_ok, reported by GET /hub so a standby can
    step in when the hub cannot draw) and logs only on transitions."""

    TIMEOUT_S = 2.0

    def __init__(self, base: str, headers: dict | None = None, opener=None):
        self.base = base
        self.headers = headers or {}
        self.opener = opener or OPENER       # cloud: proxy-aware; usb/wifi: never
        self.device_ok: bool | None = None   # None until the first draw/clear
        self.last_error = ""
        self.last_http_status: int | None = None

    def _note(self, ok: bool, err: str = ""):
        if ok:
            if self.device_ok is False:
                log(f"device {self.base}: reachable again")
            self.device_ok, self.last_error = True, ""
        else:
            if err != self.last_error:
                log(f"device {self.base}: {err}")
            self.device_ok, self.last_error = False, err

    def _request(self, method: str, path: str, body: bytes | None = None) -> bool:
        headers = dict(self.headers)
        if body:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=headers)
        try:
            with self.opener.open(req, timeout=self.TIMEOUT_S) as response:
                self.last_http_status = response.status
                self._note(True)
                return True
        except urllib.error.HTTPError as e:
            self.last_http_status = e.code
            if e.code == 409:   # reachable; an active focus session owns the screen
                self._note(True)
            else:
                self._note(False, f"HTTP {e.code} on {method} {path}")
            return False
        except OSError as e:
            self.last_http_status = None
            self._note(False, f"{type(e).__name__}: {e}"[:120])
            return False  # unplugged / offline; retried on the normal cadence

    def draw(self, payload: dict) -> bool:
        return self._request("POST", "/display/draw", json.dumps(payload).encode())

    def clear(self, app_name: str) -> bool:
        return self._request(
            "DELETE", "/display/draw?application_name=" + urllib.parse.quote(app_name)
        )

    def get_json(self, path: str) -> dict | None:
        req = urllib.request.Request(self.base + path, headers=self.headers)
        try:
            with self.opener.open(req, timeout=self.TIMEOUT_S) as r:
                return json.loads(r.read())
        except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
            return None

    def reachable(self) -> bool:
        """Cheap liveness check that never touches the canvas."""
        return self.get_json("/version") is not None


class SourceAddressHTTPHandler(urllib.request.HTTPHandler):
    """Keep USB device traffic off VPNs that also advertise 10.0.4.0/24."""

    def __init__(self, source_address: str):
        super().__init__()
        self.source_address = source_address

    def http_open(self, request):
        def connection(host, **kwargs):
            return http.client.HTTPConnection(
                host, source_address=(self.source_address, 0), **kwargs,
            )
        return self.do_open(connection, request)


def usb_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        SourceAddressHTTPHandler(USB_SOURCE_IP),
    )

def make_transport() -> HttpTransport:
    kind = os.environ.get("BUSYBAR_TRANSPORT", "usb")
    if kind == "usb":
        if STANDBY:
            sys.exit("a standby cannot use the hub's USB link: set BUSYBAR_TRANSPORT=wifi "
                     "(BUSYBAR_DEVICE, BUSYBAR_TOKEN) - setup_claude.py install --standby ...")
        return HttpTransport("http://10.0.4.20/api", opener=usb_opener())
    if kind == "wifi":
        host = os.environ.get("BUSYBAR_DEVICE", "")
        legacy = os.environ.get("BUSYBAR_HOST", "")
        if not host and re.fullmatch(r"[0-9.]+|.+\.local", legacy):
            log("BUSYBAR_HOST as the Bar's address is deprecated: set BUSYBAR_DEVICE "
                "(BUSYBAR_HOST now names this computer)")
            host = legacy
        host = re.sub(r"^https?://", "", host).strip().rstrip("/")
        if not host:
            sys.exit("wifi transport needs BUSYBAR_DEVICE (the Bar's LAN IP or name)")
        headers = {}
        if os.environ.get("BUSYBAR_TOKEN"):
            headers["x-api-token"] = os.environ["BUSYBAR_TOKEN"]
        t = HttpTransport(f"http://{host}/api", headers)
        t.TIMEOUT_S = 4.0
        return t
    if kind == "cloud":
        token = os.environ.get("BUSYBAR_TOKEN")
        if not token:
            sys.exit("cloud transport needs BUSYBAR_TOKEN (API token from the BUSY app)")
        t = HttpTransport("https://api.busy.app/busybar",
                          {"authorization": f"Bearer {token}"},
                          opener=urllib.request.build_opener())   # the internet: proxies apply
        t.TIMEOUT_S = 8.0
        return t
    if kind == "ble":
        sys.exit("BLE transport is designed but not implemented yet - see docs/EXTENDING.md")
    sys.exit(f"unknown BUSYBAR_TRANSPORT {kind!r} (usb|wifi|cloud)")


# --------------------------------------------------------------------------
# Session store (normalized records only)
# --------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}
        self.tombstones: dict = {}   # key -> (rev, when): mirrored sessions that ended
        self.ended_rev: dict = {}    # key -> rev for the tombstone we mirror ourselves
        self.dirty = threading.Event()
        self.on_report = None        # standby: HubLink.enqueue (called outside the lock)

    def report(self, source: str, session_id: str, fields: dict,
               mirrored: bool = False) -> bool:
        """Merge a normalized report. `fields` may contain: state, label,
        label_color, context_pct, quotas, badges, host, host_tag, ttl_s,
        ended - and, from a standby's mirror only: rev, lease_s, focus_ts,
        state_ts, last_active (the hub derives these from ages)."""
        key = f"{source}:{session_id}"
        now = time.time()
        created = False
        with self.lock:
            for k in [k for k, (_, when) in self.tombstones.items() if now - when > 300]:
                del self.tombstones[k]
                self.ended_rev.pop(k, None)
            rev = fields.get("rev")
            if rev is not None:
                # Mirrors can arrive late or twice; a record only moves forward.
                dead = self.tombstones.get(key)
                cur = self.sessions.get(key)
                if (dead and rev <= dead[0]) or (cur and rev < cur["rev"]):
                    return False
            if fields.get("ended"):
                s = self.sessions.pop(key, None)
                if rev is not None:
                    self.tombstones[key] = (rev, now)
                elif s is not None:
                    self.ended_rev[key] = s["rev"] + 1
            else:
                created = key not in self.sessions
                s = self.sessions.setdefault(key, {
                    "source": source, "state": "IDLE", "state_ts": 0.0,
                    "last_active": 0.0, "focus_ts": 0.0, "seen_ts": 0.0,
                    "label": None, "label_color": None,
                    "context_pct": None, "quotas": None, "badges": None,
                    "host": None, "host_tag": None, "ttl_s": DEFAULT_TTL_S,
                    "rev": 0, "mirrored": False, "lease_s": None,
                })
                if "state" in fields:
                    new, prev = fields["state"], s["state"]
                    # Attention events: the user spoke to this session, it
                    # wants the user, or a new task just started in it.
                    # Only these move the display between sessions.
                    if new in ATTENTION_STATES or (
                            new == "WORKING" and prev in ("IDLE", "COMPLETE")):
                        s["focus_ts"] = now
                    s["state"] = new
                    s["state_ts"] = fields.get("state_ts", now)
                if "focus_ts" in fields:
                    s["focus_ts"] = fields["focus_ts"]   # mirrored: the origin decided
                for k in ("label", "label_color", "context_pct", "quotas",
                          "badges", "host", "host_tag", "ttl_s"):
                    if k in fields:
                        s[k] = fields[k]
                s["lease_s"] = fields.get("lease_s") if mirrored else None
                s["last_active"] = fields.get("last_active", now)
                s["seen_ts"] = now
                s["mirrored"] = mirrored
                s["rev"] = rev if rev is not None else max(s["rev"] + 1, int(now * 1000))
        if self.on_report is not None and not mirrored:
            self.on_report(source, session_id)
        self.dirty.set()
        return created   # a mirror that created a record must re-send its state

    def live_keys(self) -> list:
        """(source, session_id) of every session that originated here."""
        with self.lock:
            return [(s["source"], k[len(s["source"]) + 1:])
                    for k, s in self.sessions.items() if not s["mirrored"]]

    def drop_mirrored(self):
        with self.lock:
            for k in [k for k, s in self.sessions.items() if s["mirrored"]]:
                del self.sessions[k]

    def active_session(self) -> dict | None:
        now = time.time()
        with self.lock:
            for key in [k for k, s in self.sessions.items()
                        if now - s["last_active"] > s.get("ttl_s", DEFAULT_TTL_S)
                        or (s.get("lease_s") and now - s["seen_ts"] > s["lease_s"])]:
                del self.sessions[key]
            if not self.sessions:
                return None
            # The display follows attention, not chatter: among the sessions
            # doing something, the one the user last talked to wins; a
            # background session surfaces only once the focused one is idle.
            return dict(max(self.sessions.values(), key=lambda s: (
                effective_state(s) != "IDLE", s["focus_ts"], s["last_active"])))

STORE = Store()
STOP = threading.Event()   # set to make the daemon exit (signals, POST /shutdown)
REDRAW = threading.Event() # POST /redraw: repaint (or clear) the whole canvas
AI_MONITOR: ai_status.Monitor | None = None
DEVICE_INPUT_LOCK = threading.Lock()
DEVICE_MODE: str | None = None
DEVICE_INPUT_CONNECTED = False
DEVICE_INPUT_ERROR = ""


# --------------------------------------------------------------------------
# Standby role: mirror every session to the hub, render only while it is down
# --------------------------------------------------------------------------

MIRROR_FIELDS = ("label", "label_color", "context_pct", "quotas", "badges",
                 "host", "host_tag", "ttl_s")
RENDER_LOCK = threading.Lock()   # one canvas transaction at a time (a yield waits on it)
DRAWN = threading.Event()        # what is on the Bar right now was painted by us


class HubLink:
    """Standby role (README: "When the hub sleeps").

    Mirrors every session that originates here to the hub as full
    /v1/report records: coalesced per session, delivered in order, `state`
    only when it changed since the hub last acknowledged it (so a hub of
    any version sees exactly the transitions a direct client would have
    sent), ages instead of timestamps (clock skew between computers does
    not matter, LAN latency does not either), a lease so the hub forgets
    us if we vanish, refreshed by a heartbeat.

    Decides when this daemon may paint the Bar: after HUB_DOWN_FAILURES
    probes fail in a row while the Bar itself still answers (evidence, not
    wall clock - a laptop waking from sleep never takes over by mistake),
    or when the hub says it cannot reach the Bar. When the hub is back:
    resync, ask it to repaint, then yield - in that order and under
    RENDER_LOCK, so no draw of ours lands after the hub's repaint."""

    def __init__(self, hub: str, token: str, transport: HttpTransport):
        self.hub = hub
        self.transport = transport
        self.headers = {"X-Busybar-Mirror": "1"}
        if token:
            self.headers["X-Busybar-Token"] = token
        self.lock = threading.Lock()
        self.queue: dict = {}        # (source, session_id) -> seq, insertion-ordered
        self.seq = 0
        self.sent_state: dict = {}   # key -> state the hub has acknowledged
        self.wake = threading.Event()
        self.failures = 0            # consecutive failed probes/deliveries
        self.last_ok = time.time()
        self.last_probe = 0.0
        self.last_heartbeat = time.time()
        self.last_error = ""
        self.hub_info: dict = {}     # last GET /hub body ({} for an older hub)
        self.hub_instance = None
        self.legacy_hub = False      # answered 404 to GET /hub: probe /health instead
        self.rejects: dict = {}      # path -> last 4xx text, until that path succeeds
        self.device_down = 0         # consecutive probes with the hub's device_ok false
        self.takeover = False        # True: this daemon paints the Bar
        self.rendering = False
        self.disabled = ""           # why the link refuses to run, if it does
        self._style_warned = False
        self._no_net = False

    def enqueue(self, source: str, session_id: str):
        with self.lock:
            self.seq += 1
            self.queue[(source, session_id)] = self.seq
        self.wake.set()

    def hub_down(self) -> bool:
        return max(self.failures, self.device_down) >= HUB_DOWN_FAILURES

    def status(self) -> dict:
        return {"hub": self.hub, "up": not self.hub_down(), "takeover": self.takeover,
                "rendering": self.rendering, "queued": len(self.queue),
                "failures": self.failures,
                "last_ok_age_s": round(time.time() - self.last_ok, 1),
                "hub_instance": self.hub_instance, "hub_style": self.hub_info.get("style"),
                "hub_device_ok": self.hub_info.get("device_ok"),
                "last_error": self.last_error, "rejected": dict(self.rejects),
                "legacy_hub": self.legacy_hub, "disabled": self.disabled}

    def _ok(self, path: str):
        self.failures, self.last_ok, self.last_error = 0, time.time(), ""
        self._no_net = False
        self.rejects.pop(path, None)

    def _request(self, method: str, path: str, body: bytes | None = None):
        """-> (status, json). status 0 = no answer at all (network)."""
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.hub + path, data=body, method=method,
                                     headers=headers)
        try:
            with OPENER.open(req, timeout=HUB_TIMEOUT_S) as r:
                raw = r.read()
            self._ok(path)
            try:
                return 200, (json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                return 200, {}
        except urllib.error.HTTPError as e:
            # The hub answered: it is up even when it rejects us. Retrying a
            # 4xx would not help, so the caller treats it as consumed. Said
            # once per path, until that path succeeds again.
            self.failures, self.last_ok, self._no_net = 0, time.time(), False
            err = f"hub answered {e.code} on {path}"
            if e.code == 401:
                err += " - BUSYBAR_HUB_TOKEN differs from the hub's"
            elif e.code == 404:
                err += " - the hub runs an older daemon.py (update and restart it)"
            if self.rejects.get(path) != err:
                log(err)
            self.rejects[path] = err
            return e.code, {}
        except OSError as e:
            self.last_error = f"{type(e).__name__}: {e}"[:120]
            return 0, {}

    def _payload(self, source: str, session_id: str):
        key = f"{source}:{session_id}"
        now = time.time()
        with STORE.lock:
            s = STORE.sessions.get(key)
            if s is None:
                return ({"source": source, "session_id": session_id, "ended": True,
                         "rev": STORE.ended_rev.get(key, int(now * 1000))}, None)
            p = {"source": source, "session_id": session_id, "rev": s["rev"],
                 "lease_s": MIRROR_LEASE_S,
                 "focus_age_s": max(0.0, now - s["focus_ts"]) if s["focus_ts"] else None,
                 "state_age_s": max(0.0, now - s["state_ts"]),
                 "active_age_s": max(0.0, now - s["last_active"]),
                 **{k: s[k] for k in MIRROR_FIELDS}}
            if s["state"] != self.sent_state.get(key):
                p["state"] = s["state"]
            return p, s["state"]

    def flush(self) -> bool:
        """Deliver queued records in order; False at the first network
        failure (the record stays queued and is retried later)."""
        while True:
            with self.lock:
                if not self.queue:
                    return True
                key, seq = next(iter(self.queue.items()))
            payload, state = self._payload(*key)
            code, reply = self._request("POST", "/v1/report", json.dumps(payload).encode())
            if code == 0 or code >= 500:
                return False
            skey = f"{key[0]}:{key[1]}"
            if code < 300:
                if state is None:
                    self.sent_state.pop(skey, None)
                else:
                    self.sent_state[skey] = state
            with self.lock:
                if self.queue.get(key) == seq:   # nothing newer arrived meanwhile
                    del self.queue[key]
            if code < 300 and reply.get("created") and "state" not in payload:
                # The hub had forgotten this session (lease, its own sleep):
                # it now holds it as IDLE. Send the state again.
                self.sent_state.pop(skey, None)
                self.enqueue(*key)

    def resync(self):
        """Re-mirror everything live, transitions included."""
        self.sent_state.clear()
        for source, sid in STORE.live_keys():
            self.enqueue(source, sid)

    def probe(self) -> bool:
        self.last_probe = time.time()
        info = {}
        if self.legacy_hub:
            code = self._request("GET", "/health")[0]
        else:
            code, info = self._request("GET", "/hub")
            if code == 404:   # older hub: alive, tells us nothing more
                self.legacy_hub = True
                code = self._request("GET", "/health")[0]
        if code == 0:
            return False
        if info:
            self._learn(info)
        return True

    def _learn(self, info: dict):
        self.hub_info = info
        inst = info.get("instance")
        if inst == INSTANCE:
            self.disabled = "BUSYBAR_HUB points at this very daemon"
        elif info.get("role") == "standby":
            self.disabled = "the hub is itself a standby: two standbys never paint anything"
        if self.disabled:
            log(f"standby link disabled: {self.disabled}")
            return
        if self.hub_instance is not None and inst != self.hub_instance:
            log("hub restarted: resyncing every session")
            self.resync()
        self.hub_instance = inst
        # The hub cannot reach its Bar: counts like unreachability, with the
        # same debounce (one failed USB draw must not start a Wi-Fi takeover).
        self.device_down = self.device_down + 1 if info.get("device_ok") is False else 0
        style = info.get("style")
        if style and style != STYLE and not self._style_warned:
            self._style_warned = True
            log(f"hub renders style {style!r}, this standby {STYLE!r}: "
                f"set BUSYBAR_STYLE the same on both computers")

    def _failed(self):
        """A probe or delivery got no answer. Count it only if the Bar still
        answers; otherwise our own network is gone and we could not draw."""
        if self.transport.reachable():
            self.failures += 1
            self._no_net = False
        else:
            self.failures = 0
            if not self._no_net:
                log("neither the hub nor the Bar answer: assuming our own network is down")
            self._no_net = True

    def loop(self, stop: threading.Event):
        last_tick = time.time()
        while not stop.is_set() and not self.disabled:
            self.wake.clear()
            now = time.time()
            if now - last_tick > SUSPEND_GAP_S:
                # We slept. Evidence from before does not count, a takeover
                # from before is over (three fresh failures re-arm it), and
                # the hub may have forgotten us meanwhile: re-mirror everything.
                self.failures = self.device_down = 0
                self.takeover = False
                self.resync()
            last_tick = now
            # Once something failed, look again sooner: takeover and the
            # hub's return are both decided by consecutive evidence.
            poll = HUB_POLL_S if self.failures == 0 and not self.takeover else 1.0
            if self.takeover:
                if now - self.last_probe >= poll and self.probe() and not self.hub_down():
                    with RENDER_LOCK:   # waits for any draw of ours in flight
                        self.resync()
                        if self.flush():
                            self._request("POST", "/redraw", b"{}")
                            self.takeover = False
                            log("hub is back: resynced, yielding the display")
                    STORE.dirty.set()
            else:
                if self.queue:
                    ok = self.flush()   # deliveries double as probes
                elif now - self.last_probe >= poll:
                    ok = self.probe()
                else:
                    ok = True
                if not ok:
                    self._failed()
                elif now - self.last_heartbeat > MIRROR_HEARTBEAT_S:
                    self.last_heartbeat = now
                    for source, sid in STORE.live_keys():
                        self.enqueue(source, sid)   # lease refresh
                if self.hub_down():
                    self.takeover = True
                    log("hub unreachable: taking over the display" if self.failures
                        else "hub cannot reach the Bar: taking over the display")
                    REDRAW.set()   # wipe the hub's stale frame before our first one
                    STORE.dirty.set()
            self.wake.wait(timeout=poll)

HUBLINK: HubLink | None = None
TRANSPORT: HttpTransport | None = None
ROLE = "standby" if STANDBY else "hub" if "0.0.0.0" in LISTEN_ADDRS else "local"


def effective_state(sess: dict) -> str:
    state = sess["state"]
    if state == "COMPLETE" and time.time() - sess["state_ts"] > COMPLETE_HOLD_S:
        return "IDLE"
    return state


def status_snapshot() -> dict:
    """Normalized active view: what renderers (and GET /status) consume."""
    sess = STORE.active_session()
    if sess is None:
        return {"source": None, "state": "IDLE", "label": None,
                "label_color": None, "context_pct": None, "quotas": None,
                "age_s": None}
    now = time.time()
    quotas = []
    for q in sess.get("quotas") or []:
        left = q.get("left_pct")
        # A quota window whose reset time has passed is back to full.
        if q.get("resets_at") and q["resets_at"] <= now:
            left = 100
        quotas.append({"name": q.get("name", ""), "left_pct": left,
                       "resets_at": q.get("resets_at")})
    return {
        "source": sess["source"],
        "state": effective_state(sess),
        "label": sess.get("label"),
        "label_color": sess.get("label_color"),
        "context_pct": sess.get("context_pct"),
        "quotas": quotas or None,
        "badges": sess.get("badges"),
        "host": sess.get("host"),
        "host_tag": sess.get("host_tag"),
        "age_s": round(now - sess["last_active"], 1),
    }


# --------------------------------------------------------------------------
# Claude Code adapter: statusline JSON + hook states -> normalized reports.
# All Claude-specific semantics live here.
# --------------------------------------------------------------------------

# Straight from the Claude Code theme palette:
# inactive / permission / warning / fastMode / effortUltra
CLAUDE_EFFORT_COLORS = {
    "low": "#999999FF", "medium": "#99CCFFFF", "high": "#FFC107FF",
    "xhigh": "#FF7814FF", "max": "#AF87FFFF",
}


def shorten_model_label(name: str, effort: str, max_px: int) -> str:
    """Fit "<model name> <effort>" into max_px: keep the effort word, chip
    letters off the longest alphabetic word of the name first ("Fable 5"
    -> "Fabl 5"), so version numbers survive as long as possible."""
    words = name.split()

    def label() -> str:
        return f"{' '.join(words)} {effort}".strip()

    while words and est_width(label()) > max_px:
        # chip the longest alphabetic word down to 3 letters; then drop
        # trailing words (versions, suffixes); then chip down to 1 letter
        for floor in (3, 1):
            alpha = [i for i, w in enumerate(words) if len(w) > floor and w[-1].isalpha()]
            if alpha:
                i = max(alpha, key=lambda i: len(words[i]))
                words[i] = words[i][:-1]
                break
            if floor == 3 and len(words) > 1:
                words.pop()
                break
        else:
            words.pop()
    return label()


def claude_statusline_report(data: dict, reserve_px: int = 0) -> dict:
    model = (data.get("model") or {})
    name = model.get("display_name") or model.get("id") or ""
    effort = (data.get("effort") or {}).get("level") or ""

    label = shorten_model_label(name, effort, LABEL_MAX_PX - reserve_px)

    ctx = data.get("context_window") or {}
    context_pct = ctx.get("used_percentage")
    if context_pct is None and ctx.get("remaining_percentage") is not None:
        context_pct = 100 - ctx["remaining_percentage"]

    quotas = []
    rl = data.get("rate_limits") or {}
    for qname, key in (("5h", "five_hour"), ("7d", "seven_day")):
        w = rl.get(key) or {}
        if w.get("used_percentage") is not None:
            quotas.append({
                "name": qname,
                "left_pct": max(0, round(100 - w["used_percentage"])),
                "resets_at": w.get("resets_at"),
            })

    fields = {"context_pct": context_pct, "quotas": quotas or None}
    if label:
        fields["label"] = label
        fields["label_color"] = CLAUDE_EFFORT_COLORS.get(effort, LABEL_FALLBACK_COLOR)
    return fields


# --------------------------------------------------------------------------
# Renderer (normalized fields only)
# --------------------------------------------------------------------------

# The device's small font is proportional (measured on-device); errs wide.
_NARROW = set("iljI.,;:' ")


def est_width(text: str) -> int:
    w = 0
    for ch in text:
        if ch in _NARROW:
            w += 3
        elif ch.isdigit():
            w += 4
        elif ch.isupper() or ch in "MWmw%":
            w += 5
        else:
            w += 4
    return w


def host_tag_kind(tag) -> tuple[str, str]:
    """Protocol `host_tag`: "#RRGGBB[AA]" -> ("flag", color): a 2x5 flag in
    the free columns left of the label (costs no text space); up to two
    printable ASCII chars -> ("text", chars): drawn after the label (the
    model name is shortened to make room); anything else -> ("", "")."""
    if not isinstance(tag, str):
        return "", ""
    tag = "".join(ch for ch in tag.strip() if 0x21 <= ord(ch) <= 0x7E)
    if re.fullmatch(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", tag):
        return "flag", _norm_color(tag, HOST_TAG_COLOR)
    return ("text", tag[:2]) if tag else ("", "")


def bar_color(used: float) -> str:
    if used >= 90:
        return "#FF2020FF"
    if used >= 80:
        return "#FF6A00FF"
    if used >= 50:
        return "#FFB000FF"
    return "#20C040FF"


def quota_bar_color(left: float) -> str:
    if left <= 10:
        return "#FF2020FF"
    if left <= 25:
        return "#FF6A00FF"
    if left <= 50:
        return "#FFB000FF"
    return "#20C040FF"


def week_progress_pct(quota: dict | None, now: float | None = None) -> float | None:
    """How far the current seven-day window has advanced toward its reset."""
    if not quota or not isinstance(quota.get("resets_at"), (int, float)):
        return None
    now = time.time() if now is None else now
    seconds_left = quota["resets_at"] - now
    if seconds_left <= 0:
        return 0.0
    seconds_left = min(WEEK_SECONDS, seconds_left)
    return 100.0 * (1.0 - seconds_left / WEEK_SECONDS)


def astra_availability(path: str | None = None, now: float | None = None) -> dict:
    """Read the small, non-secret state written by the Astra Watch plugin."""
    path = path or ASTRA_STATE_PATH
    now = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
        modified_at = os.path.getmtime(path)
    except (OSError, json.JSONDecodeError):
        state = "unknown"
        payload = {}
        modified_at = None
    else:
        state = payload.get("last_state")
        if state not in ("waiting", "hidden", "available", "error"):
            state = "unknown"
        elif ASTRA_STALE_S > 0 and now - modified_at > ASTRA_STALE_S:
            state = "stale"
    age_s = max(0.0, now - modified_at) if modified_at is not None else None
    return {
        "target": payload.get("target", "gpt-6-astra"),
        "state": state,
        "color": ASTRA_COLORS[state],
        "last_checked_at": payload.get("last_checked_at"),
        "catalog_fetched_at": payload.get("catalog_fetched_at"),
        "client_version": payload.get("client_version"),
        "model_count": payload.get("model_count"),
        "visibility": payload.get("visibility"),
        "age_s": round(age_s, 1) if age_s is not None else None,
        "next_check_s": (max(0, round(ASTRA_CHECK_INTERVAL_S - age_s))
                         if age_s is not None else None),
        "check_interval_s": round(ASTRA_CHECK_INTERVAL_S),
        "stale": state == "stale",
    }


def request_astra_refresh(now: float | None = None) -> tuple[bool, str]:
    """Start a non-inference catalog refresh, with a short debounce."""
    global ASTRA_REFRESHED_AT
    now = time.time() if now is None else now
    with ASTRA_REFRESH_LOCK:
        if now - ASTRA_REFRESHED_AT < 15:
            return True, "already refreshing"
        if not os.path.isfile(ASTRA_WATCH_SCRIPT):
            return False, "Astra Watch script is not installed"
        try:
            subprocess.Popen(
                [sys.executable, ASTRA_WATCH_SCRIPT, "check", "--json"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            return False, f"could not start Astra Watch: {exc}"
        ASTRA_REFRESHED_AT = now
    return True, "refresh started"


def x_pulse_status(now: float | None = None) -> dict:
    if X_PULSE_MONITOR is None:
        return {
            "enabled": False, "state": "disabled", "official": False,
            "label": "X community pulse",
        }
    return X_PULSE_MONITOR.status(now)


def request_x_pulse_refresh(*, force: bool = False, now: float | None = None) -> bool:
    if X_PULSE_MONITOR is None:
        return False
    return X_PULSE_MONITOR.request_refresh(force=force, now=now)


def astra_watch_status() -> dict:
    status = astra_availability()
    status["x_pulse"] = x_pulse_status()
    return status


def mark_astra_app_seen(now: float | None = None):
    """Record a status poll from the on-device app for button routing/debug."""
    global ASTRA_APP_LAST_SEEN, ASTRA_APP_REQUESTS
    with ASTRA_APP_LOCK:
        ASTRA_APP_LAST_SEEN = time.monotonic() if now is None else now
        ASTRA_APP_REQUESTS += 1
    STORE.dirty.set()


def astra_app_status(now: float | None = None) -> dict:
    now = time.monotonic() if now is None else now
    with ASTRA_APP_LOCK:
        age = now - ASTRA_APP_LAST_SEEN if ASTRA_APP_LAST_SEEN else None
        requests = ASTRA_APP_REQUESTS
    return {
        "active": age is not None and age <= ASTRA_APP_ACTIVE_S,
        "last_seen_age_s": round(max(0.0, age), 1) if age is not None else None,
        "requests": requests,
    }


def handle_astra_app_poll(now: float | None = None):
    """Refresh once when the on-device app opens, then serve cheap polls."""
    was_active = astra_app_status(now)["active"]
    mark_astra_app_seen(now)
    if not was_active:
        request_astra_refresh()
    request_x_pulse_refresh()


def _norm_color(c, fallback: str) -> str:
    if not isinstance(c, str) or not c.startswith("#") or len(c) not in (7, 9):
        return fallback
    return (c + "FF") if len(c) == 7 else c


def _rect(eid, x, y, w, h, color):
    return {"id": eid, "type": "rectangle", "display": "front",
            "x": x, "y": y, "width": w, "height": h,
            "border_width": 0,  # undocumented; defaults to 1px white border
            "fill": "solid", "fill_colors": [color], "timeout": TEXT_TIMEOUT_S}


def _text(eid, x, y, align, text, color):
    return {"id": eid, "type": "text", "display": "front",
            "x": x, "y": y, "align": align,
            "text": text, "font": FONT, "color": color, "timeout": TEXT_TIMEOUT_S}


def anim_element(state: str, badges=None, astra_state: str | None = None) -> dict:
    path = STATE_ANIMS.get(state, "idle.anim")
    if astra_state == "available":
        path = "astra.anim"
    elif state == "WORKING" and "fast" in (badges or []):
        path = "work_fast.anim"
    return {"id": "ring", "type": "animation", "display": "front",
            "x": 0, "y": 0, "path": path,
            "loop": True, "timeout": ANIM_TIMEOUT_S}


def avatar_element(state: str) -> dict:
    return {"id": "avatar", "type": "animation", "display": "front",
            "x": AVATAR_X, "y": AVATAR_Y,
            "path": AVATAR_ANIMS.get(state, "claw_idle.anim"),
            "loop": True, "timeout": ANIM_TIMEOUT_S}


def device_canvas_allowed() -> bool:
    with DEVICE_INPUT_LOCK:
        mode_allowed = DEVICE_MODE not in ("APPS", "SETTINGS")
    return mode_allowed and not astra_app_status()["active"]


def handle_device_input_event(event: tuple) -> bool:
    """Track the mode selector and route OK to an active Astra Watch app."""
    global DEVICE_MODE
    if not event:
        return False
    if event[0] == "button":
        if event[1:3] == (0, 0) and astra_app_status()["active"]:
            request_astra_refresh()
            request_x_pulse_refresh(force=True)
            return True
        return False
    if event[0] != "switch":
        return False
    mode = {0: "BUSY", 1: "CUSTOM", 2: "OFF", 3: "APPS", 4: "SETTINGS"}.get(
        event[1], "UNKNOWN",
    )
    with DEVICE_INPUT_LOCK:
        changed = mode != DEVICE_MODE
        DEVICE_MODE = mode
    if changed:
        STORE.dirty.set()
    return changed


def device_input_loop(input_url: str, stop: threading.Event,
                      source_address: str | None = None):
    """Observe switch events using the same local WebSocket as the SDK."""
    global DEVICE_INPUT_CONNECTED, DEVICE_INPUT_ERROR
    logged_error = ""
    while not stop.is_set():
        sock = None
        try:
            sock = ai_status._ws_connect(input_url, source_address=source_address)
            ai_status._ws_send(sock, 1, b'{"enable":true}')
            with DEVICE_INPUT_LOCK:
                DEVICE_INPUT_CONNECTED = True
                DEVICE_INPUT_ERROR = ""
            if logged_error:
                log("BUSY mode observer connected again")
                logged_error = ""
            fragments = bytearray()
            fragment_opcode = None
            next_ping = time.monotonic() + ai_status.WS_PING_INTERVAL_S
            while not stop.is_set():
                if time.monotonic() >= next_ping:
                    ai_status._ws_send(sock, 9, os.urandom(4))
                    next_ping = time.monotonic() + ai_status.WS_PING_INTERVAL_S
                try:
                    opcode, final, payload = ai_status._ws_recv(sock)
                except TimeoutError:
                    continue
                if opcode == 8:
                    raise ConnectionError("WebSocket closed")
                if opcode == 9:
                    ai_status._ws_send(sock, 10, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode in (1, 2):
                    fragments = bytearray(payload)
                    fragment_opcode = opcode
                elif opcode == 0 and fragment_opcode is not None:
                    fragments.extend(payload)
                else:
                    continue
                if not final:
                    continue
                if fragment_opcode == 2:
                    for event in ai_status.parse_input_events(bytes(fragments)):
                        handle_device_input_event(event)
                fragments.clear()
                fragment_opcode = None
        except (OSError, ValueError, ConnectionError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:160]
            with DEVICE_INPUT_LOCK:
                DEVICE_INPUT_CONNECTED = False
                DEVICE_INPUT_ERROR = error
            if error != logged_error and not stop.is_set():
                log(f"BUSY mode observer unavailable: {error}")
                logged_error = error
            stop.wait(timeout=3)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def info_elements(status: dict, astra: dict | None = None) -> list[dict]:
    """Text rows + weekly gauges for a normalized snapshot (ring/avatar are
    separate animation elements)."""
    avatar = STYLE == "avatar"
    label_max = (AVATAR_X - 4 - 5) if avatar else LABEL_MAX_PX
    elements = []
    state = status["state"]

    kind, tag = host_tag_kind(status.get("host_tag"))
    if kind == "text":
        label_max -= est_width(tag) + 2  # the tag sits right after the label
    label = status.get("label") or ""
    label = "".join(ch for ch in label if 0x20 <= ord(ch) <= 0x7E)  # ASCII-only font
    while label and est_width(label) > label_max:
        label = label[:-1]
    if label:
        elements.append(_text("model", 3, 0, "top_left", label,
                              _norm_color(status.get("label_color"), LABEL_FALLBACK_COLOR)))
    x = 3 + est_width(label)
    # Host tag (which computer this session lives on). Both variants are
    # always sent - blank/transparent when unused - so a stale tag never
    # outlives a switch to another session (element ids can't change type).
    elements.append(_text("htag", x + 2, 0, "top_left",
                          tag if kind == "text" else " ", HOST_TAG_COLOR))
    if kind == "text":
        x += 2 + est_width(tag)
    elements.append(_rect("hflag", 1, 2, 2, 5, tag if kind == "flag" else "#00000000"))
    quotas = [q for q in (status.get("quotas") or []) if q.get("left_pct") is not None]
    weekly = next((q for q in quotas if str(q.get("name", "")).lower()
                   in ("7d", "week", "weekly", "wk")), quotas[-1] if quotas else None)
    week_progress = week_progress_pct(weekly)
    if avatar:
        # Vertical week-progress gauge between the text column and avatar.
        elements.append(_rect("ctrack", AVATAR_X - 4, 1, 2, 14, BAR_TRACK_COLOR))
        if week_progress is not None and week_progress > 0:
            fh = max(1, min(14, round(14 * week_progress / 100)))
            elements.append(_rect("cfill", AVATAR_X - 4, 15 - fh, 2, fh,
                                  bar_color(week_progress)))
    else:
        elements.append(_rect("ctrack", BAR_X, BAR_Y, BAR_W, BAR_H, BAR_TRACK_COLOR))
        if week_progress is not None and week_progress > 0:
            fill = max(1, min(BAR_W, round(BAR_W * week_progress / 100)))
            elements.append(_rect("cfill", BAR_X, BAR_Y, fill, BAR_H,
                                  bar_color(week_progress)))

    if weekly:
        left = max(0.0, min(100.0, float(weekly["left_pct"])))
        color = quota_bar_color(left)
        fill = max(1, min(QUOTA_BAR_W, round(QUOTA_BAR_W * left / 100)))
        elements.append(_text("usage", 3, 15, "bottom_left", "W", color))
        elements.append(_rect("qtrack", QUOTA_BAR_X, QUOTA_BAR_Y,
                              QUOTA_BAR_W, QUOTA_BAR_H, BAR_TRACK_COLOR))
        elements.append(_rect("qfill", QUOTA_BAR_X, QUOTA_BAR_Y,
                              fill, QUOTA_BAR_H,
                              color if left > 0 else "#00000000"))
    else:
        # Keep element ids and types stable while clearing a previous quota.
        elements.append(_text("usage", 3, 15, "bottom_left", " ", QUOTA_COLOR))
        elements.append(_rect("qtrack", QUOTA_BAR_X, QUOTA_BAR_Y,
                              QUOTA_BAR_W, QUOTA_BAR_H, "#00000000"))
        elements.append(_rect("qfill", QUOTA_BAR_X, QUOTA_BAR_Y,
                              1, QUOTA_BAR_H, "#00000000"))

    # Tiny 3x5 pixel "A" between the quota gauge and state word. The plugin
    # state maps to: waiting gray, server-visible-but-hidden amber, available
    # green, and stale/error red. It is transparent when Astra Watch is absent.
    astra_color = (astra or astra_availability())["color"]
    elements.extend((
        _rect("astra_top", 47, 10, 1, 1, astra_color),
        _rect("astra_left", 46, 11, 1, 4, astra_color),
        _rect("astra_right", 48, 11, 1, 4, astra_color),
        _rect("astra_cross", 46, 12, 3, 1, astra_color),
    ))
    if not avatar:
        elements.append(_text("state", 69, 15, "bottom_right",
                              STATE_WORDS.get(state, state[:5]),
                              STATE_COLORS.get(state, STATE_COLORS["IDLE"])))
    return elements


THEME_ACTIVE = threading.Event()


def snapshot_watch_loop(transport: HttpTransport, stop: threading.Event):
    """Poll the device's BUSY snapshot: the currently selected theme acts
    as the on-device manual switch for the status display."""
    while not stop.is_set():
        active = False
        snap = transport.get_json("/busy/snapshot")
        if snap:
            s = snap.get("snapshot") or {}
            active = (s.get("busy_bar_settings") or {}).get("theme") == THEME_NAME
        if active != THEME_ACTIVE.is_set():
            log(f"claude theme {'selected' if active else 'deselected'} on device")
            (THEME_ACTIVE.set if active else THEME_ACTIVE.clear)()
            STORE.dirty.set()
        stop.wait(SNAPSHOT_POLL_S)


def render_loop(transport: HttpTransport, stop: threading.Event):
    last_texts = None
    last_texts_ts = 0.0
    last_anim = None
    last_anim_ts = 0.0
    last_tick = time.time()
    ai_overlay_was_active = False
    while not stop.is_set():
        STORE.dirty.clear()
        now = time.time()
        if now - last_tick > SUSPEND_GAP_S:
            # This computer slept. Mirrors we hold are stale (their standby
            # resyncs within seconds); the canvas is repainted from scratch.
            log(f"resumed after {now - last_tick:.0f}s: repainting")
            STORE.drop_mirrored()
            REDRAW.set()
        last_tick = now

        # Do not queue lower-priority USB draw calls underneath the provider
        # overlay. The firmware rejects or defers them, holding the shared
        # render lock long enough to make a four-second carousel look stuck.
        # Once the overlay clears, repaint the normal canvas from scratch.
        ai_overlay_active = AI_MONITOR is not None and AI_MONITOR.drawn
        if ai_overlay_active:
            ai_overlay_was_active = True
            STORE.dirty.wait(timeout=0.25)
            continue
        if ai_overlay_was_active:
            last_anim, last_texts = None, None
            ai_overlay_was_active = False

        with RENDER_LOCK:
            sess = STORE.active_session()
            want = False
            if sess is not None:
                idle_expired = (
                    IDLE_CLEAR_AFTER_S > 0
                    and effective_state(sess) == "IDLE"
                    and now - max(sess["state_ts"], sess["last_active"]) > IDLE_CLEAR_AFTER_S
                )
                gate_ok = THEME_ACTIVE.is_set() if RENDER_MODE == "theme" else True
                gate_ok = gate_ok and device_canvas_allowed()
                if HUBLINK is not None:
                    gate_ok = gate_ok and HUBLINK.takeover   # standby: only while the hub is out
                want = gate_ok and not idle_expired
            force = REDRAW.is_set()
            if force:
                REDRAW.clear()
                last_anim, last_texts = None, None
            if HUBLINK is not None:
                HUBLINK.rendering = want

            if not want:
                if DRAWN.is_set() or force:
                    # A standby yielding leaves the canvas to the hub, which
                    # repaints it (POST /redraw); everyone else wipes their own.
                    if HUBLINK is None or HUBLINK.takeover:
                        transport.clear(APP_NAME)
                    DRAWN.clear()
                    last_anim, last_texts = None, None
            else:
                if force:
                    # Leftovers first: a sleeping hub's frame, another style's ids.
                    transport.clear(APP_NAME)
                status = status_snapshot()
                astra = astra_availability()
                anim = anim_element(
                    status["state"], status.get("badges"), astra["state"],
                )
                if anim["path"] != last_anim or now - last_anim_ts > ANIM_REFRESH_S:
                    anims = [anim]
                    if STYLE == "avatar":
                        anims.append(avatar_element(status["state"]))
                    if transport.draw({"application_name": APP_NAME,
                                       "priority": DRAW_PRIORITY, "elements": anims}):
                        last_anim, last_anim_ts = anim["path"], now
                        DRAWN.set()
                texts = info_elements(status, astra)
                encoded = json.dumps(texts, sort_keys=True)
                if encoded != last_texts or now - last_texts_ts > KEEPALIVE_S:
                    if transport.draw({"application_name": APP_NAME,
                                       "priority": DRAW_PRIORITY, "elements": texts}):
                        last_texts, last_texts_ts = encoded, now
                        DRAWN.set()
        STORE.dirty.wait(timeout=0.5)

# --------------------------------------------------------------------------
# Report/status server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code: int, body: bytes = b"{}"):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not HUB_TOKEN or self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        return hmac.compare_digest(self.headers.get("X-Busybar-Token", ""), HUB_TOKEN)

    def _host_fields(self) -> dict:
        """Which computer a report comes from (report.py / report.sh headers)."""
        fields = {}
        host = (self.headers.get("X-Busybar-Host") or "").strip()[:64]
        if host:
            fields["host"] = host
        tag = (self.headers.get("X-Busybar-Host-Tag") or "").strip()[:9]
        if tag:
            fields["host_tag"] = tag
        return fields

    def do_GET(self):
        if self.path in ("/status", "/v1/status"):
            self._reply(200, json.dumps(status_snapshot()).encode())
        elif self.path == "/astra":
            if self.client_address[0] == "10.0.4.20":
                handle_astra_app_poll()
            self._reply(200, json.dumps(astra_watch_status()).encode())
        elif self.path == "/hub":
            self._reply(200, json.dumps({
                "ok": True, "instance": INSTANCE, "role": ROLE, "style": STYLE,
                "render_mode": RENDER_MODE,
                "device_mode": DEVICE_MODE,
                "device_input": {
                    "connected": DEVICE_INPUT_CONNECTED,
                    "error": DEVICE_INPUT_ERROR,
                },
                "device_ok": TRANSPORT.device_ok if TRANSPORT else None,
                "device_error": TRANSPORT.last_error if TRANSPORT else "",
                "rendering": DRAWN.is_set(),
                "astra": astra_availability(),
                "x_pulse": x_pulse_status(),
                "astra_app": astra_app_status(),
                "ai_status": (AI_MONITOR.status() if AI_MONITOR else
                              {"enabled": False}),
            }).encode())
        elif self.path == "/standby":
            if HUBLINK is None:
                self._reply(404, b'{"error":"not a standby daemon"}')
            else:
                self._reply(200, json.dumps(HUBLINK.status()).encode())
        elif self.path == "/health":
            with STORE.lock:
                snapshot = {
                    key: {k: s[k] for k in ("source", "state", "state_ts",
                                            "last_active", "focus_ts", "host", "host_tag")}
                    for key, s in STORE.sessions.items()
                }
            self._reply(200, json.dumps(snapshot).encode())
        else:
            self._reply(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        if not self._authorized():
            self._reply(401, b'{"error":"missing or wrong X-Busybar-Token"}')
            return
        host_fields = self._host_fields()

        if parsed.path == "/astra/refresh":
            # The on-device Astra app lives at 10.0.4.20. This action only
            # refreshes the personalized model catalog; it never runs a model.
            if self.client_address[0] not in ("127.0.0.1", "::1", "10.0.4.20"):
                self._reply(403, b'{"error":"local device only"}')
                return
            started, message = request_astra_refresh()
            self._reply(202 if started else 503, json.dumps({
                "ok": started, "message": message,
            }).encode())

        elif parsed.path == "/v1/report":
            # The provider-agnostic reporting endpoint. See docs/EXTENDING.md.
            source = str(data.get("source") or "")[:32]
            session_id = str(data.get("session_id") or "")[:128]
            if not source or not session_id:
                self._reply(400, b'{"error":"source and session_id are required"}')
                return
            fields: dict = {}
            if data.get("ended"):
                fields["ended"] = True
            state = data.get("state")
            if state is not None:
                if state not in STATES:
                    self._reply(400, json.dumps(
                        {"error": f"state must be one of {list(STATES)}"}).encode())
                    return
                fields["state"] = state
            if "label" in data:
                fields["label"] = str(data["label"] or "")[:64] or None
            if "label_color" in data:
                fields["label_color"] = str(data["label_color"] or "") or None
            if "context_pct" in data:
                v = data["context_pct"]
                try:
                    fields["context_pct"] = (max(0.0, min(100.0, float(v)))
                                             if v is not None else None)
                except (TypeError, ValueError, OverflowError):
                    pass
            if "quotas" in data:
                qs = data["quotas"] or []
                fields["quotas"] = [
                    {"name": str(q.get("name", ""))[:6],
                     "left_pct": max(0, min(100, round(q["left_pct"]))),
                     "resets_at": q.get("resets_at")}
                    for q in qs[:4] if isinstance(q, dict) and q.get("left_pct") is not None
                ] or None
            if "badges" in data:
                bs = data["badges"] or []
                fields["badges"] = [str(b)[:12] for b in bs[:4]] or None
            if "ttl_s" in data and data["ttl_s"]:
                try:
                    fields["ttl_s"] = max(10.0, min(30 * 86400.0, float(data["ttl_s"])))
                except (TypeError, ValueError, OverflowError):
                    pass
            # Mirror fields (standby daemons only, header X-Busybar-Mirror).
            # Ages, never timestamps: the other computer's clock may be
            # seconds off, the LAN is not.
            mirrored = self.headers.get("X-Busybar-Mirror") == "1"
            now = time.time()
            if mirrored:
                for age_key, ts_key in (("focus_age_s", "focus_ts"),
                                        ("state_age_s", "state_ts"),
                                        ("active_age_s", "last_active")):
                    if data.get(age_key) is not None:
                        try:
                            fields[ts_key] = now - max(0.0, float(data[age_key]))
                        except (TypeError, ValueError, OverflowError):
                            pass
                if data.get("rev") is not None:
                    try:
                        fields["rev"] = int(data["rev"])
                    except (TypeError, ValueError, OverflowError):
                        pass
                if data.get("lease_s"):
                    try:
                        fields["lease_s"] = max(10.0, min(10 * MIRROR_LEASE_S,
                                                          float(data["lease_s"])))
                    except (TypeError, ValueError, OverflowError):
                        pass
            fields.update(host_fields)
            if data.get("host"):
                fields["host"] = str(data["host"])[:64]
            if "host_tag" in data:
                fields["host_tag"] = str(data["host_tag"] or "")[:9] or None
            created = STORE.report(source, session_id, fields, mirrored=mirrored)
            self._reply(200, json.dumps({"ok": True, "created": created}).encode())

        elif parsed.path == "/state":
            # claude adapter: hook events
            sid = data.get("session_id") or "unknown"
            state = urllib.parse.parse_qs(parsed.query).get("state", ["WORKING"])[0]
            if state == "ENDED":
                STORE.report("claude-code", sid, {"ended": True})
            elif state in STATES:
                STORE.report("claude-code", sid, {"state": state, **host_fields})
            self._reply(200)

        elif parsed.path == "/shutdown":
            # Loopback only. Exit cleanly; the next Claude Code activity
            # respawns the daemon with a fresh env.sh (setup_claude.py uses it).
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._reply(403)
                return
            self._reply(200, b'{"ok":true}')
            STOP.set()

        elif parsed.path == "/redraw":
            # A standby yielded the canvas back: repaint everything, or clear
            # it when there is nothing to show (the ids are shared).
            REDRAW.set()
            STORE.dirty.set()
            self._reply(200, b'{"ok":true}')

        elif parsed.path == "/statusline":
            # claude adapter: statusline payload
            sid = data.get("session_id") or "unknown"
            kind, tag = host_tag_kind(host_fields.get("host_tag"))
            reserve = est_width(tag) + 2 if kind == "text" else 0
            STORE.report("claude-code", sid,
                         {**claude_statusline_report(data, reserve), **host_fields})
            self._reply(200)

        else:
            self._reply(404)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port,
    # which would break the single-instance guarantee.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        # Clients cap their wait at about a second; while we were asleep
        # they gave up, and on wake their replies hit closed sockets.
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


def serve_on(addr: str) -> ThreadingHTTPServer | None:
    try:
        server = _Server((addr, LISTEN_PORT), Handler)
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def bind_loop(addr: str, stop: threading.Event, servers: list):
    """Secondary listeners. The USB interface (10.0.4.21) exists only while
    the device is plugged in - keep retrying so the device app can always
    reach us."""
    while not stop.is_set():
        server = serve_on(addr)
        if server:
            servers.append(server)
            log(f"listener up on {addr}:{LISTEN_PORT}")
            return
        stop.wait(timeout=30)


def main():
    global HUBLINK, TRANSPORT, AI_MONITOR
    stop = STOP
    servers = []
    TRANSPORT = transport = make_transport()
    if STANDBY:
        HUBLINK = HubLink(HUB_URL, HUB_TOKEN, transport)
        STORE.on_report = HUBLINK.enqueue

    primary = serve_on(LISTEN_ADDRS[0])
    if primary is None:
        return 0  # another instance already owns the port
    servers.append(primary)
    for addr in LISTEN_ADDRS[1:]:
        threading.Thread(target=bind_loop, args=(addr, stop, servers), daemon=True).start()
    if STANDBY:
        threading.Thread(target=HUBLINK.loop, args=(stop,), daemon=True).start()

    def shutdown(*_):
        stop.set()
        for s in servers:
            threading.Thread(target=s.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if RENDER_MODE != "off":
        local_input_url = ""
        if os.environ.get("BUSYBAR_TRANSPORT", "usb") != "cloud":
            local_input_url = ai_status.input_stream_url(
                transport.base,
                transport.headers.get("x-api-token", ""),
            )
            threading.Thread(
                target=device_input_loop,
                args=(local_input_url, stop,
                      USB_SOURCE_IP if transport.base.startswith("http://10.0.4.20/")
                      else None),
                daemon=True,
            ).start()
        threading.Thread(target=render_loop, args=(transport, stop), daemon=True).start()
        if AI_STATUS_ENABLED:
            AI_MONITOR = ai_status.Monitor(
                transport,
                RENDER_LOCK,
                url=AI_STATUS_URL,
                x_url=X_STATUS_URL,
                google_url=GOOGLE_STATUS_URL,
                input_url=local_input_url,
                poll_s=AI_STATUS_POLL_S,
                should_render=lambda: (
                    (HUBLINK is None or HUBLINK.takeover)
                    and device_canvas_allowed()
                ),
                logger=log,
            )
            threading.Thread(target=AI_MONITOR.run, args=(stop,), daemon=True).start()
    if RENDER_MODE == "theme":
        threading.Thread(target=snapshot_watch_loop, args=(transport, stop), daemon=True).start()
    log(f"listening on {LISTEN_ADDRS[0]}:{LISTEN_PORT}, render_mode={RENDER_MODE}, "
        f"style={STYLE}, transport={os.environ.get('BUSYBAR_TRANSPORT', 'usb')}, "
        f"ai_status={'on' if AI_STATUS_ENABLED else 'off'}, "
        f"x_pulse={X_PULSE_BACKEND if X_PULSE_ENABLED else 'off'}, "
        f"x_llm={X_PULSE_LLM_MODEL if X_PULSE_LLM_ENABLED else 'off'}, "
        f"hub_token={'on' if HUB_TOKEN else 'off'}, "
        f"role={ROLE}{' for ' + HUB_URL if STANDBY else ''}")

    while not stop.is_set():
        stop.wait(timeout=3600)
    if RENDER_MODE != "off":
        # Wait for any final monitor draw, then remove only our two canvases.
        with RENDER_LOCK:
            if AI_MONITOR is not None:
                transport.clear(ai_status.APP_NAME)
                AI_MONITOR.drawn = False
            if DRAWN.is_set():
                transport.clear(APP_NAME)
    return 0

if __name__ == "__main__":
    sys.exit(main())
