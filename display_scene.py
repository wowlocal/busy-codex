"""Batch native display elements and remember only acknowledged screen state.

Callers provide a fresh scene on each wake-up. Failed or superseded updates
are never queued, so reconnecting draws the current state instead of a backlog.
The device continues playing native animations between these small updates.
"""
from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class DrawUpdate:
    group: str
    elements: list[dict]
    signature: str
    observed_at: float


class DrawCache:
    def __init__(self, application_name: str, priority: int):
        self.application_name = application_name
        self.priority = priority
        self._sent: dict[str, tuple[str, float]] = {}

    def reset(self):
        """Invalidate the cache after clearing, yielding or waking from sleep."""
        self._sent.clear()

    def pending(self, group: str, elements: list[dict], now: float,
                refresh_after: float) -> DrawUpdate | None:
        signature = json.dumps(elements, sort_keys=True, separators=(',', ':'))
        previous = self._sent.get(group)
        if previous and previous[0] == signature and now - previous[1] < refresh_after:
            return None
        return DrawUpdate(group, elements, signature, now)

    def draw(self, transport, *updates: DrawUpdate | None) -> bool:
        changes = [update for update in updates if update is not None]
        if not changes:
            return False
        elements = [element for update in changes for element in update.elements]
        if not transport.draw({'application_name': self.application_name,
                               'priority': self.priority, 'elements': elements}):
            return False
        for update in changes:
            self._sent[update.group] = (update.signature, update.observed_at)
        return True
