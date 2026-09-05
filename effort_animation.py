"""Native 72x16 effort transitions: sapphire currents, cyan glints, white type."""
import math

W, H, FPS, FRAMES = 72, 16, 25, 65
TEXT_H = 12
LEVELS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
GLYPHS = {
 'A': ('01110','10001','10001','11111','10001','10001','10001'),
 'D': ('11110','10001','10001','10001','10001','10001','11110'),
 'E': ('11111','10000','10000','11110','10000','10000','11111'),
 'G': ('01110','10001','10000','10111','10001','10001','01110'),
 'H': ('10001','10001','10001','11111','10001','10001','10001'),
 'I': ('111','010','010','010','010','010','111'),
 'L': ('10000','10000','10000','10000','10000','10000','11111'),
 'M': ('10001','11011','10101','10101','10001','10001','10001'),
 'N': ('10001','11001','11001','10101','10011','10011','10001'),
 'O': ('01110','10001','10001','10001','10001','10001','01110'),
 'R': ('11110','10001','10001','11110','10100','10010','10001'),
 'T': ('11111','00100','00100','00100','00100','00100','00100'),
 'U': ('10001','10001','10001','10001','10001','10001','01110'),
 'W': ('10001','10001','10001','10101','10101','10101','01010'),
 'X': ('10001','10001','01010','00100','01010','10001','10001'),
}


def word_pixels(word):
    pixels = set()
    offset = 0
    for letter in word:
        glyph = GLYPHS[letter]
        source_w = len(glyph[0])
        width = source_w + 2
        for y in range(TEXT_H - 1):
            for x in range(width):
                if glyph[y * 7 // (TEXT_H - 1)][x * source_w // width] == '1':
                    # Two-pixel strokes stay readable on the real LED matrix.
                    pixels.update(((offset + x, y), (offset + x + 1, y),
                                   (offset + x, y + 1)))
        offset += width + 3
    return pixels, offset - 2


def smoothstep(value):
    value = max(0., min(1., value))
    return value * value * (3 - 2 * value)


def frames(effort, direction=1, n=FRAMES, entering=True):
    word, width = word_pixels(effort.upper())
    result = []
    rank = LEVELS.index(effort) / (len(LEVELS) - 1)
    for f in range(n):
        t = f / FPS
        # Each step adds a little energy, while keeping one visual style:
        # calmer cyan currents below, faster sapphire/violet glints above.
        flow_t = t * (.85 + .40 * rank)
        intro = max(0, 1 - f / 7) ** 3
        outro = smoothstep((f - (FRAMES - 11)) / 10)
        shift = round(direction * (22 * intro - 10 * outro))
        text = {(x + (W - width) // 2 + shift, y + 2) for x, y in word}
        glow = {(x + dx, y + dy) for x, y in text
                for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0))} - text
        buf = bytearray()
        for y in range(H):
            for x in range(W):
                # Broad moving blue wave and a narrower bent cyan light band.
                u = x / W
                wave = .5 + .5 * math.sin(u * 5.5 - flow_t * 2.3 + y * .12)
                crest_x = (flow_t * 29 + 14 * math.sin(y * .20 + flow_t)) % 110 - 19
                crest = math.exp(-((x - crest_x) / (13 - 2 * rank)) ** 2)
                ripple = (.5 + .5 * math.sin(x * .19 + y * .48 - flow_t * 4)) ** 3
                # A continuous field across the whole display, including the
                # gaps between letters; no rectangular shading behind the word.
                r = 3 + 7 * wave + rank * (12 + 14 * wave + 20 * ripple)
                g = 14 + 35 * wave + 65 * crest + 13 * ripple - 9 * rank
                b = 38 + 82 * wave + 78 * crest + 20 * ripple + 9 * rank
                # Thin streaks glide outside the word, with a soft trailing glow.
                for k in range(5):
                    sy = (1, 14, 2, 13, 0)[k]
                    head = ((flow_t * (25 + k * 4) + k * 19) % 94) - 10
                    distance = head - x
                    if y == sy and 0 <= distance < 9:
                        light = (1 - distance / 9) ** 3 * (.65 + .35 * rank)
                        r += 115 * light; g += 165 * light; b += 150 * light
                # A moving highlight around the perimeter, rather than a static frame.
                if x in (0, W-1) or y in (0, H-1):
                    g += 14 + 24 * crest; b += 24 + 25 * crest
                if (x, y) in glow:
                    r, g, b = max(r, 8), max(g, 64), max(b, 145)
                if (x, y) in text:
                    gleam = .5 + .5 * math.sin(x * .08 - flow_t * 3)
                    r, g, b = 210 + 40 * gleam, 240 + 15 * gleam, 255
                # Fade the native overlay over the still-live quota screen.
                # The last frame is transparent, so hiding never flashes black.
                reveal = smoothstep(f / 7) if entering else 1
                alpha = round(255 * reveal * (1 - outro))
                if alpha == 0:
                    buf.extend((0, 0, 0, 0))
                else:
                    buf.extend((min(255, round(b)), min(255, round(g)), min(255, round(r)), alpha))
        result.append(bytes(buf))
    return result


def filename(effort, direction, entering=True):
    return f'effort_v3_{effort}_{"up" if direction > 0 else "down"}_{"in" if entering else "change"}.anim'
