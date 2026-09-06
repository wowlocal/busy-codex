"""Pixel-exact 72x16 effort labels over increasingly energetic native effects."""
import math

from pixel_fonts import EFFORT_BOLD
from pixel_ui import Canvas, SlideFade

W, H, FPS, FRAMES = 72, 16, 25, 45
TEXT_H = EFFORT_BOLD.height
DURATION_S = FRAMES / FPS
FADE_FRAMES = 8
LEVELS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')

TRANSITION = SlideFade(FRAMES, FADE_FRAMES)

# Color, speed, and motion geometry all change, rather than just tinting a wave.
PALETTES = (
    ((9, 15, 24), (42, 60, 80)),       # none: quiet slate
    ((3, 22, 24), (14, 95, 90)),       # minimal: teal drift
    ((3, 28, 21), (20, 145, 95)),      # low: green aurora
    ((2, 25, 42), (15, 145, 210)),     # medium: cyan streams
    ((4, 16, 48), (35, 100, 245)),     # high: blue racing currents
    ((24, 7, 45), (155, 50, 245)),     # xhigh: violet double helix
    ((37, 14, 2), (245, 135, 20)),     # max: gold shockwaves
    ((32, 3, 40), (225, 45, 200)),     # ultra: magenta plasma + ice sparks
)
SPEEDS = (.2, .4, .65, 1., 1.4, 2., 2.7, 3.6)


def word_pixels(word):
    """Compatibility helper for tools that inspect the effort label geometry."""
    mask = EFFORT_BOLD.layout(word)
    return mask.pixels, mask.width


def background(rank, x, y, t):
    speed = SPEEDS[rank]
    phase = t * speed
    edge = (abs(y - 7.5) / 7.5) ** 1.6
    wave = .5 + .5 * math.sin(x * .085 - phase * 2 + y * .18)
    base, accent = PALETTES[rank]
    intensity = .12 + .26 * wave
    # The center is smoothly quieter over the entire width, including gaps
    # between letters. No rectangular backing plate or luminous glyph outline.
    field = .38 + .62 * edge
    spark = 0.
    if rank <= 2:
        crest = math.exp(-((y - (7 + 4 * math.sin(x * .065 - phase))) / 3) ** 2)
        intensity += (.04 + .08 * rank) * crest
    elif rank <= 4:
        streams = (.5 + .5 * math.sin(x * .16 + y * .45 - phase * 4)) ** 5
        intensity += (.2 + .15 * (rank - 3)) * streams
    elif rank == 5:
        # Two counter-running strands have a recognisable braided silhouette.
        strand = 5.5 * math.sin(x * .11 - phase * 2.2)
        braid = max(math.exp(-((y - 7.5 - strand) / 1.4) ** 2),
                    math.exp(-((y - 7.5 + strand) / 1.4) ** 2))
        intensity += .7 * braid
    elif rank == 6:
        # Golden pressure rings expand out of the centre, with ember trails.
        radius = math.hypot((x - 35.5) * .5, (y - 7.5) * 1.7)
        ring = (.5 + .5 * math.cos(radius * .8 - phase * 4)) ** 9
        intensity += .95 * ring
    else:
        # Layered plasma and paired outward shock fronts; no full-screen flash.
        plasma = (.5 + .5 * math.sin(x * .19 + math.sin(y * .8 + phase * 3)
                                    - phase * 4)) ** 3
        front = (.5 + .5 * math.cos(abs(x - 35.5) * .24 - phase * 3)) ** 12
        intensity += .75 * plasma + .55 * front
    # Higher levels get more, faster comets, concentrated in the free margins.
    for k in range(max(0, rank - 1)):
        lane = (0, 15, 1, 14, 0, 15)[k]
        travel = t * (10 + speed * 17 + k * 3)
        head = (travel + k * 19) % 92 - 10
        if k % 2:
            head = W - 1 - head
        distance = (head - x) * (-1 if k % 2 else 1)
        if y == lane and 0 <= distance < 8:
            spark = max(spark, (1 - distance / 8) ** 2 * (.2 + rank * .11))
    rgb = [a + b * intensity * field for a, b in zip(base, accent)]
    if rank == 7:
        ice = (.5 + .5 * math.sin(x * .15 + phase * 4)) ** 12 * edge
        rgb = [c + v * ice for c, v in zip(rgb, (20, 130, 160))]
    return tuple(min(255, round(c + s * spark)) for c, s in zip(rgb, (160, 190, 210)))


def _render(rank, mask, frame, direction, entering):
    alpha, shift = TRANSITION.at(frame, direction, entering)
    canvas = Canvas(W, H)
    if alpha:
        canvas.paint(lambda x, y: background(rank, x, y, frame / FPS), alpha)
        canvas.draw_mask(mask, (W - mask.width) // 2 + shift,
                         (H - mask.height) // 2, (245, 250, 255), alpha)
    return canvas.to_bgra()


def frame(effort, index, direction=1, entering=True):
    """Render one deterministic frame without computing preceding frames."""
    return _render(LEVELS.index(effort), EFFORT_BOLD.layout(effort.upper()),
                   index, direction, entering)


def frames(effort, direction=1, n=FRAMES, entering=True):
    mask = EFFORT_BOLD.layout(effort.upper())
    rank = LEVELS.index(effort)
    # Fully readable after 80 ms; subsequent detents replace the label
    # immediately at its resting position without replaying its entrance.
    return [_render(rank, mask, f, direction, entering) for f in range(n)]


def filename(effort, direction, entering=True):
    return f'effort_v4_{effort}_{"up" if direction > 0 else "down"}_{"in" if entering else "change"}.anim'
