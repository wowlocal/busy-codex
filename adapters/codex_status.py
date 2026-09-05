#!/usr/bin/env python3
"""Codex CLI adapter for the busybar daemon.

Zero-config and rename-proof: everything is derived from what Codex
itself writes (config.toml defaults + the newest session rollout under
~/.codex/sessions). No model-name tables anywhere:

  - label:   the raw model id, prettified by GENERIC rules only
             ("gpt-5.6-sol" -> "5.6 Sol"; a future "gpt-7-luna" ->
             "7 Luna" with zero changes here), plus the reasoning effort
  - badges:  service_tier other than default becomes a badge
             ("fast" selects the yellow high-speed working contour)
  - context: last_token_usage.total_tokens / model_context_window
  - quotas:  Codex's own rate_limits windows, named from window_minutes
             (600 -> "10h", 10080 -> "7d") - names survive plan changes
  - state:   Codex task_started / task_complete lifecycle events

Usage:
    python3 adapters/codex_status.py            # loop, report every 2s
    python3 adapters/codex_status.py --once -v  # single probe, print it
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import report  # noqa: E402  (daemon/hub address + host headers from env.sh)
import codex_focus

DAEMON = report.BASE + "/v1/report"
CODEX_HOME = pathlib.Path.home() / ".codex"
SESSIONS = CODEX_HOME / "sessions"

QUIET_RECHECK_S = 8  # one final read after the rollout stops changing
COMPLETE_S = 120     # fallback for old rollouts without lifecycle events
POLL_S = 0.25

VENDOR_PREFIXES = ("gpt-", "chatgpt-", "openai-")


def _empty_snapshot() -> dict:
    return {
        "model": None, "effort": None, "tier": None,
        "info": None, "limits": None, "state": None,
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


def window_name(minutes: float) -> str:
    hours = minutes / 60
    return f"{round(hours)}h" if hours < 48 else f"{round(hours / 24)}d"


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


def newest_rollout() -> pathlib.Path | None:
    target = report.ENV.get("BUSYBAR_CODEX_THREAD_ID", "") or codex_focus.FOCUS.current()
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
        if isinstance(payload.get("rate_limits"), dict):
            snapshot["limits"] = payload["rate_limits"]


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


def probe() -> dict | None:
    rollout = newest_rollout()
    defaults = config_defaults()
    if rollout is None and not defaults:
        return None

    model = defaults.get("model")
    effort = defaults.get("model_reasoning_effort")
    tier = defaults.get("service_tier")
    context_pct = None
    quotas = None
    state = "IDLE"
    session_id = "config"

    if rollout is not None:
        session_id = rollout.stem.split("rollout-")[-1][:64]
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
        limits = snapshot["limits"]
        if info:
            window = info.get("model_context_window")
            last = (info.get("last_token_usage") or {}).get("total_tokens")
            if window and last:
                context_pct = round(min(100, last * 100 / window), 1)

        if limits:
            quotas = []
            for k in ("primary", "secondary"):
                w = limits.get(k) or {}
                if w.get("used_percent") is not None and w.get("window_minutes"):
                    quotas.append({
                        "name": window_name(w["window_minutes"]),
                        "left_pct": max(0, round(100 - w["used_percent"])),
                        "resets_at": w.get("resets_at"),
                    })
            quotas = quotas or None

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
        "control_thread_id": (meta.get("id") if meta.get("originator") == "Codex Desktop"
                              and meta.get("source") == "vscode" else None),
        "label": label, "context_pct": context_pct, "quotas": quotas,
        "badges": badges, "ttl_s": 600,
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


def _emit(verbose: bool):
    report = probe()
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
        _emit(verbose)
        return
    # Report only while the rollout changes, plus one quiet recheck. State is
    # taken from lifecycle events, so a long silent reasoning/tool interval
    # remains WORKING until Codex actually writes task_complete.
    last_stamp = None
    previous = None
    last_report_at = 0
    quiet_rechecked = False
    while True:
        rollout = newest_rollout()
        stat = rollout.stat() if rollout else None
        stamp = (rollout, stat.st_mtime_ns, stat.st_size) if stat else None
        if rollout != previous:
            if previous:
                post({"source": "codex", "session_id": previous.stem.split("rollout-")[-1][:64],
                      "ended": True})
            previous = rollout
        if stamp != last_stamp or time.time() - last_report_at >= 20:
            last_stamp = stamp
            last_report_at = time.time()
            quiet_rechecked = False
            _emit(verbose)
        elif (not quiet_rechecked and stat is not None
              and time.time() - stat.st_mtime > QUIET_RECHECK_S):
            _emit(verbose)
            quiet_rechecked = True
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
