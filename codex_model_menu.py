"""Explicit model preview; browsing never changes a session's settings."""
import copy

TIMEOUT_S = 12


def model_settings(state, model, levels, default):
    from codex_effort import model_effort, effort_settings
    current = model_effort(state)[1]
    effort = current if current in levels else default
    if effort not in levels:
        raise ValueError('Model has no supported default effort')
    result = effort_settings(state, effort)
    result['model'] = model
    if 'collaborationMode' in result:
        result['collaborationMode']['settings']['model'] = model
    return result


class ModelMenu:
    def __init__(self, choices, current, now):
        self.choices = copy.deepcopy(choices)
        self.index = next((i for i, item in enumerate(choices) if item['model'] == current), 0)
        self.until = now + TIMEOUT_S

    def rotate(self, delta, now):
        self.index = max(0, min(len(self.choices) - 1, self.index + int(delta)))
        self.until = now + TIMEOUT_S

    def snapshot(self):
        return {**self.choices[self.index], 'index': self.index, 'count': len(self.choices)}
