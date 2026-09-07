"""Resolve the selected model's advertised Fast tier, never a global default."""
import copy
import json
from pathlib import Path


def current_tier(state):
    return (state.get('latestThreadSettings') or {}).get('serviceTier')


def is_fast(state):
    settings = state.get('latestThreadSettings') or {}
    if 'serviceTier' not in settings:
        return None
    tier = settings['serviceTier']
    return tier is not None and tier in ('fast', 'priority', settings.get('fastServiceTier'))


def toggle_settings(state, home, kind):
    settings = state.get('latestThreadSettings') or {}
    if 'serviceTier' not in settings:
        raise ValueError('Fast control needs a current settings snapshot; restart the updated native CLI')
    current = current_tier(state)
    if kind == 'cli':
        fast_tier = settings.get('fastServiceTier')
    else:
        data = json.loads((Path(home) / 'models_cache.json').read_text())
        model = settings.get('model') or state.get('latestModel')
        entry = next((m for m in data['models'] if m.get('slug') == model), {})
        if current is None:
            current = entry.get('default_service_tier')
        fast_tier = next((tier['id'] for tier in entry.get('service_tiers', [])
                          if str(tier.get('name', '')).strip().lower() in ('fast', 'priority')
                          or tier.get('id') in ('priority', 'fast')), None)
    if not fast_tier:
        raise ValueError('Fast mode is not advertised for this model or account')
    enabled = current not in ('fast', 'priority', fast_tier)
    # `default` explicitly disables tier routing, even on Fast-by-default models.
    update = {'serviceTier': fast_tier if enabled else 'default'}
    mode = settings.get('collaborationMode') or state.get('latestCollaborationMode')
    if mode:
        update['collaborationMode'] = copy.deepcopy(mode)
    return update, enabled
