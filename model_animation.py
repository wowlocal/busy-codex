"""Reusable native model cards: explicit visual classes, never inferred from effort.

Classes are editorial UX metadata, not benchmark scores. Unknown IDs stay
neutral; extending the catalog needs only a profile entry, no new renderer.
"""
import math
import re
from dataclasses import dataclass

from pixel_ui import Canvas

W, H, FPS, FRAMES = 72, 16, 25, 75


@dataclass(frozen=True)
class Profile:
    rank: int
    color: tuple
    motif: str = ''

    @property
    def hex(self):
        return '#' + ''.join(f'{c:02X}' for c in self.color) + 'FF'


PROFILES = {
    'neutral': Profile(0, (133, 161, 185)),
    'luna': Profile(1, (85, 217, 184)),
    'spark': Profile(1, (255, 171, 74)),
    'terra': Profile(2, (70, 182, 255)),
    'classic': Profile(2, (111, 143, 255)),
    'sol': Profile(3, (194, 119, 255)),
    'astra': Profile(4, (175, 137, 255), 'galaxy'),
    'sol6': Profile(3, (255, 151, 48), 'flame'),
    'luna6': Profile(1, (255, 222, 132), 'moon'),
}
MODEL_PROFILES = {
    'gpt-6-astra': 'astra', 'gpt-5.6-sol': 'sol',
    'gpt-6-sol': 'sol6', 'gpt-6-luna': 'luna6',
    'gpt-5.6-terra': 'terra', 'gpt-5.6-luna': 'luna',
    'gpt-5.5': 'classic', 'gpt-5.3-codex-spark': 'spark',
}


def profile_key(model):
    return MODEL_PROFILES.get(model.lower(), 'neutral')


def model_color(model):
    return PROFILES[profile_key(model)].hex


def label(model, name=None):
    # Preserve version and distinguishing suffix, not the repeated vendor name.
    value = re.sub(r'^gpt[- ]', '', name or model, flags=re.I)
    value = re.sub(r'[- ]codex(?=[- ])', '', value, flags=re.I)
    return re.sub(r'[-_]+', ' ', value).upper()


def _mix(a, b, amount):
    amount = max(0., min(1., amount))
    return tuple(round(x + (y - x) * amount) for x, y in zip(a, b))


def _spark(x, y, cx, cy, phase):
    """A pixel star with a quiet core and occasionally extended arms."""
    pulse = (.5 + .5 * math.sin(phase)) ** 4
    distance = abs(x - cx) + abs(y - cy)
    return max(0, 1 - distance / (1 + pulse)) * (.3 + .7 * pulse)


def _generation_six(profile, x, y, phase):
    # A 16x13 portrait leaves the firmware title, hints and class marks clear.
    # Use multicolor silhouettes: recognizable even without their title.
    base = tuple(round(c * .022) for c in profile.color)
    if x >= 16 or y >= 13:
        return base
    if profile.motif == 'galaxy':
        dx, dy = x - 7.5, (y - 6 + (x - 7.5) * .28) * 1.55
        radius, angle = math.hypot(dx, dy), math.atan2(dy, dx)
        arms = (.5 + .5 * math.cos(angle * 2 - radius * 1.05 + phase * 2)) ** 5
        disk = math.exp(-((radius - 3.4) / 2.6) ** 2)
        glow = .10 * math.exp(-(radius / 6) ** 2)
        energy = min(1, glow + disk * (.10 + .85 * arms))
        hue = _mix((70, 49, 210), profile.color, arms)
        color = _mix(base, hue, energy)
        core = math.exp(-(radius / 1.45) ** 2) * (.85 + .15 * math.sin(phase * 2))
        color = _mix(color, (255, 242, 211), core)
        for cx, cy, offset in ((2, 2, 0), (13, 10, math.pi)):
            color = _mix(color, (213, 230, 255), _spark(x, y, cx, cy, phase * 2 + offset))
        return color
    if profile.motif == 'flame':
        # Three independently licking tongues merge into a rounded base.
        tongues = ((5, 4.5, 1.8, 0), (8, 1.5, 2.3, 2), (11, 5, 1.7, 4))
        flame = 0.
        for center, top, width, offset in tongues:
            tip = top + .9 * math.sin(phase * 3 + offset)
            height = (y - tip) / (12 - tip)
            if 0 <= height <= 1:
                sway = (1 - height) * math.sin(phase * 3 + height * 4 + offset)
                half_width = width * math.sin(math.pi * height) ** .7 + .35
                flame = max(flame, min(1, max(0, half_width - abs(x - center - sway))))
        color = _mix(base, (244, 57, 15), flame)
        belly = math.hypot((x - 8) / 3.8, (y - 9) / 3.4)
        if belly < 1:
            flame = max(flame, min(1, (1 - belly) * 4))
            color = _mix(color, (255, 108, 18), flame)
        hot = max(0, 1 - abs(x - 8 - .4 * math.sin(phase * 3)) / 2.4)
        hot *= max(0, 1 - abs(y - 9) / 4) * flame
        color = _mix(color, (255, 244, 148), hot)
        # Embers rise, fading in and out before their loop wraps.
        for cx, offset in ((3, 0), (13, .5)):
            travel = (phase / (2 * math.pi) * 2 + offset) % 1
            ember = max(0, 1 - math.hypot(x - cx, y - (8 - travel * 7)))
            color = _mix(color, (255, 176, 50), ember * math.sin(math.pi * travel))
        return color
    # A steady crescent silhouette with a slight bob, a passing surface glint
    # and one orbiting star. Motion is deliberately slower than Sol's flame.
    dy = y - (6 + .35 * math.sin(phase))
    outer = math.hypot(x - 6.5, dy)
    cutout = math.hypot(x - 9, dy - .25)
    coverage = max(0, min(1, 5.5 - outer)) * max(0, min(1, cutout - 4.0))
    glint = .5 + .5 * math.cos((x + y) * .35 - phase)
    color = _mix(base, _mix((220, 154, 54), (255, 240, 168), glint), coverage)
    for cx, cy in ((4, 5), (6, 10)):
        if x == cx and y == cy:
            color = _mix(color, (195, 132, 40), coverage * .6)
    return _mix(color, (232, 242, 255), _spark(
        x, y, 12 + round(math.sin(phase)), 3 + round(math.cos(phase)), phase))


def background(key, x, y, frame):
    profile = PROFILES[key]
    rank = profile.rank
    phase = 2 * math.pi * (frame % FRAMES) / FRAMES
    if profile.motif:
        return _generation_six(profile, x, y, phase)
    dx, dy = x - 7.5, y - 6.5
    radius = math.hypot(dx, dy)
    angle = math.atan2(dy, dx)
    # Quiet, continuous field behind text. Motion lives in the glyph and edges.
    energy = .035 + .025 * (.5 + .5 * math.sin(x * .12 - phase))
    if x < 16 and y < 13:
        if rank == 0:  # a breathing halo, deliberately without a class rating
            shape = math.exp(-((radius - 3.8) / .85) ** 2)
            energy += shape * (.3 + .12 * math.sin(phase))
        elif rank == 1:  # one calm orbit and a moving satellite
            ring = math.exp(-((radius - 4.2) / .7) ** 2)
            energy += ring * (.25 + .65 * (.5 + .5 * math.cos(angle - phase)) ** 8)
            energy += .4 * math.exp(-(radius / 1.4) ** 2)
        elif rank == 2:  # two interlocking orbital planes
            for sign in (-1, 1):
                orbit = math.hypot(dx, dy * 1.8 + sign * dx * .6)
                energy += .45 * math.exp(-((orbit - 4.6) / .7) ** 2) * (
                    .5 + .5 * math.cos(angle * 2 + sign * phase * 2))
            energy += .7 * math.exp(-(radius / 1.5) ** 2)
        elif rank == 3:  # braided streams surrounding a concentrated core
            for sign in (-1, 1):
                strand = sign * 3.6 * math.sin(dy * .55 - phase * 2)
                energy += .7 * math.exp(-((dx - strand) / .8) ** 2)
            energy *= .6 + .4 * math.exp(-(dy / 5) ** 4)
        else:  # rotating four-point star, core and opposing orbital sparks
            star = 2.8 + 1.5 * math.cos(angle * 4 - phase * 2)
            energy += .65 * math.exp(-((radius - star) / .7) ** 2)
            energy += .9 * math.exp(-(radius / 1.8) ** 2)
            energy += .65 * math.exp(-((radius - 5.4) / .65) ** 2) * (
                .5 + .5 * math.cos(angle * 2 + phase * 4)) ** 10
    if y in (0, 14) and x > 17:
        energy += .1 * (.5 + .5 * math.sin(x * .15 - phase * max(1, rank))) ** 8
    color = tuple(min(255, round(c * energy)) for c in profile.color)
    if rank == 4 and x < 16 and energy > .65:
        color = tuple(min(255, c + round((energy - .65) * 135)) for c in color)
    return color


def frame(key, index):
    canvas = Canvas(W, H)
    canvas.paint(lambda x, y: background(key, x, y, index))
    return canvas.to_bgra()


def filename(key):
    version = 2 if PROFILES[key].motif else 1
    return f'model_v{version}_{key}.anim'


def transition_frame(key, index, direction=1, confirmed=False):
    """A short directional rim sweep; the name remains visible throughout."""
    count = 20 if confirmed else 8
    phase = index / (count - 1)
    profile = PROFILES[key]
    accent = profile.color
    def color(x, y):
        lane_x = 71 - x if profile.motif == 'galaxy' and y == 15 else x
        if confirmed:
            distance = abs(lane_x - 35.5)
            strength = max(0, 1 - abs(distance - phase * 44) / 9)
        else:
            head = phase * 90 - 9
            if direction < 0:
                head = 71 - head
            strength = max(0, 1 - abs(lane_x - head) / 9)
        if profile.motif == 'flame':
            strength *= .55 + .45 * math.sin(x * 1.7 - phase * 24) ** 2
        elif profile.motif == 'moon':
            strength = strength ** 2 * .7
        return tuple(round(c * strength * (1 - phase)) for c in accent)
    # Only the two edge lanes are opaque; don't obscure the text or its counter.
    data = bytearray(W * H * 4)
    for y in (0, 15):
        for x in range(W):
            r, g, b = color(x, y)
            i = (y * W + x) * 4
            data[i:i + 4] = bytes((b, g, r, max(r, g, b)))
    return bytes(data)


def transition_filename(key, direction=1, confirmed=False):
    stem = filename(key).removesuffix('.anim')
    return f'{stem}_{"set" if confirmed else "right" if direction > 0 else "left"}.anim'


def assets():
    for key in PROFILES:
        yield filename(key), [frame(key, i) for i in range(FRAMES)]
        for direction, confirmed in ((1, False), (-1, False), (1, True)):
            yield transition_filename(key, direction, confirmed), [
                transition_frame(key, i, direction, confirmed)
                for i in range(20 if confirmed else 8)]
