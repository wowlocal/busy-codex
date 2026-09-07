"""START button scenes: golden warp ignition and a blue deceleration wake."""
import math

from effort_animation import W, H, FPS, FRAMES, FADE_FRAMES, DURATION_S
from pixel_fonts import EFFORT_BOLD
from pixel_ui import Canvas, PixelMask, SlideFade

TRANSITION = SlideFade(FRAMES, FADE_FRAMES, enter_distance=12, leave_distance=8)
BOLT = PixelMask(8, 12, frozenset((x, y) for y, row in enumerate((
    '00001110', '00011100', '00111000', '01110000', '11111111', '11111110',
    '00011100', '00111000', '01110000', '01100000', '11000000', '10000000'))
    for x, value in enumerate(row) if value == '1'))


def background(enabled, x, y, t):
    edge = (abs(y - 7.5) / 7.5) ** 1.7
    if enabled:
        # Accelerating, perspective-stretched stars leave the central core.
        travel = 22 * t + 45 * t * t
        distance = abs(x - 35.5)
        ray = (.5 + .5 * math.sin(distance * .31 - travel * .6 + y * 1.7)) ** 12
        shock = math.exp(-((math.hypot((x - 35.5) * .4, (y - 7.5) * 1.5)
                           - t * 40) / 2.8) ** 2)
        base, accent = (28, 8, 1), (245, 126, 12)
        energy = .18 + ray * .8 + shock * 1.4
    else:
        # The same kinetic energy compresses into cool rings, then settles.
        travel = 45 * (1 - math.exp(-3.3 * t))
        radius = math.hypot((x - 35.5) * .45, (y - 7.5) * 1.7)
        ring = (.5 + .5 * math.cos(radius * .7 + travel)) ** 10
        base, accent = (2, 12, 29), (22, 145, 245)
        energy = .17 + ring * math.exp(-t * 1.5)
    rgb = [a + b * energy * (.28 + .72 * edge) for a, b in zip(base, accent)]
    # Bright warp trails stay outside the letters, not underneath a text slab.
    for k, lane in enumerate((0, 15, 1, 14)):
        head = (travel * (1 + k * .19) + k * 23) % 98 - 13
        if not enabled:
            head = 71 - head
        tail = (head - x) * (1 if enabled else -1)
        if y == lane and 0 <= tail < 13:
            strength = (1 - tail / 13) ** 2 * (1 if enabled else math.exp(-1.6 * t))
            rgb = [c + s * strength for c, s in zip(rgb, (170, 205, 240))]
    return tuple(min(255, round(value)) for value in rgb)


def frame(enabled, index, entering=True):
    alpha, shift = TRANSITION.at(index, 1 if enabled else -1, entering)
    canvas = Canvas(W, H)
    if alpha:
        canvas.paint(lambda x, y: background(enabled, x, y, index / FPS), alpha)
        mask = EFFORT_BOLD.layout('FAST' if enabled else 'NORMAL')
        width = mask.width + (12 if enabled else 0)
        x = (W - width) // 2 + shift
        if enabled:
            canvas.draw_mask(BOLT, x, 2, (255, 220, 90), alpha)
            x += 12
        canvas.draw_mask(mask, x, 2, (248, 252, 255), alpha)
    return canvas.to_bgra()


def frames(enabled, entering=True):
    return [frame(enabled, index, entering) for index in range(FRAMES)]


def filename(enabled, entering=True):
    return f'fast_v1_{"on" if enabled else "off"}_{"in" if entering else "change"}.anim'
