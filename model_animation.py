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
    'astra': Profile(4, (255, 112, 192)),
}
MODEL_PROFILES = {
    'gpt-6-astra': 'astra', 'gpt-5.6-sol': 'sol',
    'gpt-5.6-terra': 'terra', 'gpt-5.6-luna': 'luna',
    'gpt-5.5': 'classic', 'gpt-5.3-codex-spark': 'spark',
}


def profile_key(model):
    return MODEL_PROFILES.get(model.lower(), 'neutral')


def label(model, name=None):
    # Preserve version and distinguishing suffix, not the repeated vendor name.
    value = re.sub(r'^gpt[- ]', '', name or model, flags=re.I)
    value = re.sub(r'[- ]codex(?=[- ])', '', value, flags=re.I)
    return re.sub(r'[-_]+', ' ', value).upper()


def background(key, x, y, frame):
    profile = PROFILES[key]
    rank = profile.rank
    phase = 2 * math.pi * (frame % FRAMES) / FRAMES
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
    return f'model_v1_{key}.anim'


def transition_frame(key, index, direction=1, confirmed=False):
    """A short directional rim sweep; the name remains visible throughout."""
    count = 20 if confirmed else 8
    phase = index / (count - 1)
    accent = PROFILES[key].color
    def color(x, y):
        if confirmed:
            distance = abs(x - 35.5)
            strength = max(0, 1 - abs(distance - phase * 44) / 9)
        else:
            head = phase * 90 - 9
            if direction < 0:
                head = 71 - head
            strength = max(0, 1 - abs(x - head) / 9)
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
    return f'model_v1_{key}_{"set" if confirmed else "right" if direction > 0 else "left"}.anim'


def assets():
    for key in PROFILES:
        yield filename(key), [frame(key, i) for i in range(FRAMES)]
        for direction, confirmed in ((1, False), (-1, False), (1, True)):
            yield transition_filename(key, direction, confirmed), [
                transition_frame(key, i, direction, confirmed)
                for i in range(20 if confirmed else 8)]
