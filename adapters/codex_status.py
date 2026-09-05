#!/usr/bin/env python3
"""Codex Desktop/CLI adapter for the busybar daemon.

Zero-config and rename-proof: everything is derived from what Codex
itself reports (config defaults, the selected task's rollout and the
account/rateLimits/read API). No model-name tables anywhere:

  - label:   the raw model id, prettified by GENERIC rules only
             ("gpt-5.6-sol" -> "5.6 Sol"; a future "gpt-7-luna" ->
             "7 Luna" with zero changes here), plus the reasoning effort
  - badges:  service_tier other than default becomes a badge
             ("fast" selects the yellow high-speed working contour)
  - context: last_token_usage.total_tokens / model_context_window
  - quotas:  fresh account windows, independent of selected task/activity
             (600 -> "10h", 10080 -> "7d") - names survive plan changes
  - state:   Codex task_started / task_complete lifecycle events

Usage:
    python3 adapters/codex_status.py            # watch task + refresh quotas every minute
    python3 adapters/codex_status.py --once -v  # single probe, print it
"""

from __future__ import annotations

import json
import pathlib
import re
import signal
import sys
import threading
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import report  # noqa: E402  (daemon/hub address + host headers from env.sh)
import codex_focus
import codex_target
import codex_usage

DAEMON = report.BASE + "/v1/report"
CODEX_HOME = pathlib.Path(report.ENV.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
SESSIONS = CODEX_HOME / "sessions"
TARGET = codex_target.Target(CODEX_HOME)

QUIET_RECHECK_S = 8  # one final read after the rollout stops changing
COMPLETE_S = 120     # fallback for old rollouts without lifecycle events
POLL_S = 0.25

VENDOR_PREFIXES = ("gpt-", "chatgpt-", "openai-")


def _empty_snapshot() -> dict:
    return {
        "model": None, "effort": None, "tier": None,
        "info": None, "state": None,
    }


# The long-running adapter reads only newly appended JSONL records. A --once
# process starts with an empty cache and scans the current rollout once.
_ROLLOUT_CACHE = {
    "path": None,
    "offset": 0,
    "snapshot": _empty_snapshot(),
}


def prettify_model(model: str) -> str:
    """Generic prettifier - strips vendor prefixes and title-cases word
    tokens. Never matches specific model names."""
    m = model.lower()
    for p in VENDOR_PREFIXES:
        if m.startswith(p):
            model = model[len(p):]
            break
    parts = []
    for tok in re.split(r"[-_]", model):
        parts.append(tok if any(c.isdigit() for c in tok) else tok.capitalize())
    return " ".join(p for p in parts if p)


def config_defaults() -> dict:
    """Minimal TOML pluck of the keys we need (no toml dependency)."""
    out = {}
    try:
        for line in (CODEX_HOME / "config.toml").read_text().splitlines():
            m = re.match(r'\s*(model|model_reasoning_effort|service_tier)\s*=\s*"([^"]*)"', line)
            if m:
                out[m.group(1)] = m.group(2)
    except OSError:
        pass
    return out


_FOCUSED_ROLLOUTS = {}


def selected_target():
    pinned = report.ENV.get("BUSYBAR_CODEX_THREAD_ID", "")
    if pinned:
        record = next((r for r in codex_target.cli_sessions(CODEX_HOME)
                       if r.get('thread_id') == pinned), None)
        return {**record, 'kind': 'cli'} if record else {'kind': 'desktop', 'thread_id': pinned}
    return TARGET.display()


def newest_rollout(selection=None) -> pathlib.Path | None:
    target = (selection if selection is not None else selected_target()).get('thread_id')
    if target:
        path = _FOCUSED_ROLLOUTS.get(target)
        if path and path.exists():
            return path
        path = next(SESSIONS.glob(f"*/*/*/rollout-*-{target}.jsonl"), None)
        if path:
            _FOCUSED_ROLLOUTS[target] = path
        return path
    return None


def rollout_metadata(path) -> dict:
    try:
        with path.open() as stream:
            event = json.loads(stream.readline())
        return event.get("payload", {}) if event.get("type") == "session_meta" else {}
    except (OSError, ValueError):
        return {}


def _apply_event(snapshot: dict, event: dict) -> None:
    """Merge one structured rollout event into the latest known snapshot."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return

    if event.get("type") == "turn_context":
        for source_key, target_key in (("model", "model"), ("effort", "effort"),
                                       ("service_tier", "tier")):
            value = payload.get(source_key)
            if isinstance(value, str) and value:
                snapshot[target_key] = value
        return

    if event.get("type") != "event_msg":
        return
    event_type = payload.get("type")
    if event_type == "task_started":
        snapshot["state"] = "WORKING"
    elif event_type in ("task_complete", "turn_aborted", "task_cancelled", "task_canceled"):
        snapshot["state"] = "COMPLETE"
    elif event_type == "token_count":
        if isinstance(payload.get("info"), dict):
            snapshot["info"] = payload["info"]


def _rollout_snapshot(path: pathlib.Path) -> dict:
    """Incrementally parse JSONL and retain the latest structured fields."""
    stat = path.stat()
    if (_ROLLOUT_CACHE["path"] != path or stat.st_size < _ROLLOUT_CACHE["offset"]):
        _ROLLOUT_CACHE.update({
            "path": path,
            "offset": 0,
            "snapshot": _empty_snapshot(),
        })

    with path.open("rb") as stream:
        stream.seek(_ROLLOUT_CACHE["offset"])
        while True:
            position = stream.tell()
            raw = stream.readline()
            if not raw:
                break
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Do not advance past a record while Codex is still appending it.
                if not raw.endswith(b"\n"):
                    stream.seek(position)
                    break
                _ROLLOUT_CACHE["offset"] = stream.tell()
                continue
            if isinstance(event, dict):
                _apply_event(_ROLLOUT_CACHE["snapshot"], event)
            _ROLLOUT_CACHE["offset"] = stream.tell()
    return dict(_ROLLOUT_CACHE["snapshot"])


def probe(usage=None) -> dict | None:
    target = selected_target()
    rollout = newest_rollout(target)
    defaults = config_defaults()
    if rollout is None and not defaults and not target.get('model'):
        return None

    model = defaults.get("model")
    effort = defaults.get("model_reasoning_effort")
    tier = defaults.get("service_tier")
    context_pct = None
    state = "IDLE"
    session_id = target.get('thread_id') or 'config'

    if rollout is not None:
        age = time.time() - rollout.stat().st_mtime
        snapshot = _rollout_snapshot(rollout)
        state = snapshot["state"] or (
            "WORKING" if age < QUIET_RECHECK_S else
            ("COMPLETE" if age < COMPLETE_S else "IDLE")
        )
        model = snapshot["model"] or model
        effort = snapshot["effort"] or effort
        tier = snapshot["tier"] or tier

        info = snapshot["info"]
        if info:
            window = info.get("model_context_window")
            last = (info.get("last_token_usage") or {}).get("total_tokens")
            if window and last:
                context_pct = round(min(100, last * 100 / window), 1)

    if target.get('kind') == 'cli':
        model, effort = target.get('model'), target.get('effort')
        state, context_pct = target.get('state', 'IDLE'), target.get('context_pct')
        tier = next(iter(target.get('badges') or []), None)
    if not model:
        return None

    label = prettify_model(model)
    badges = None
    if tier and tier not in ("default", "standard"):
        badges = [tier]
        if tier != "fast":  # unknown tiers also get spelled out
            label += f" {tier}"
    if effort:
        label += f" {effort}"

    meta = rollout_metadata(rollout) if rollout else {}
    return {
        "source": "codex", "session_id": session_id, "state": state,
        "control_thread_id": (target['thread_id'] if target.get('kind') == 'cli' and target.get('ready')
                              else meta.get("id") if meta.get("originator") == "Codex Desktop"
                              and meta.get("source") == "vscode" else None),
        "label": label, "context_pct": context_pct,
        "badges": badges, "ttl_s": 600,
        **(usage or {}),
    }


def report_headers() -> dict:
    return dict(report.HEADERS)


def post(report: dict) -> bool:
    try:
        urllib.request.urlopen(urllib.request.Request(
            DAEMON, data=json.dumps(report).encode(), method="POST",
            headers=report_headers()), timeout=2).read()
        return True
    except OSError:
        return False


def _emit(verbose: bool, usage=None):
    report = probe(usage)
    if report:
        if verbose:
            print(json.dumps(report, ensure_ascii=False), flush=True)
        post(report)
    elif verbose:
        print("no codex data found", flush=True)


def main():
    once = "--once" in sys.argv
    verbose = "-v" in sys.argv
    if once:
        # Notify hooks leave account polling to the existing background adapter.
        # A manual --once still provides a fresh, self-contained diagnostic.
        usage = None
        if "--no-usage-refresh" not in sys.argv:
            monitor = codex_usage.Monitor(report.ENV)
            monitor.refresh()
            usage = monitor.snapshot()
        _emit(verbose, usage)
        return
    monitor = codex_usage.Monitor(report.ENV)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker = threading.Thread(target=monitor.run, args=(stop,), daemon=True)
    worker.start()
    try:
        watch(monitor, stop, verbose)
    finally:
        stop.set()
        worker.join(timeout=4)


def watch(monitor, stop, verbose):
    # Quota updates also trigger a report while the selected task is idle.
    last_stamp = None
    previous = None
    last_report_at = 0
    quiet_rechecked = False
    while not stop.is_set():
        target = selected_target()
        rollout = newest_rollout(target)
        stat = rollout.stat() if rollout else None
        session_id = target.get('thread_id') or 'config'
        stamp = (session_id, target.get('model'), target.get('effort'), target.get('state'),
                 target.get('context_pct'), stat.st_mtime_ns if stat else None, stat.st_size if stat else None)
        if session_id != previous:
            if previous:
                post({"source": "codex", "session_id": previous, "ended": True})
            previous = session_id
        if (stamp != last_stamp or monitor.changed.is_set()
                or time.time() - last_report_at >= 20):
            monitor.changed.clear()
            last_stamp = stamp
            last_report_at = time.time()
            quiet_rechecked = False
            _emit(verbose, monitor.snapshot())
        elif (not quiet_rechecked and stat is not None
              and time.time() - stat.st_mtime > QUIET_RECHECK_S):
            _emit(verbose, monitor.snapshot())
            quiet_rechecked = True
        stop.wait(POLL_S)


if __name__ == "__main__":
    main()
