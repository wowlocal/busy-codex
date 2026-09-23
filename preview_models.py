#!/usr/bin/env python3
"""Offline model-card contact sheet and GIF, with the exact native effects.

Text uses a small bitmap approximation of the firmware font; layout and colors
come from the production elements. Requires Pillow only for preview export.
"""
import argparse
from pathlib import Path

import daemon
from codex_model_menu import CONFIRMATION_S
import model_animation as animation

# A preview-only 3x5 alphabet. Device text is rendered by its built-in font.
_GLYPHS = {
    'A':'010/101/111/101/101', 'B':'110/101/110/101/110',
    'C':'011/100/100/100/011', 'D':'110/101/101/101/110',
    'E':'111/100/110/100/111', 'F':'111/100/110/100/100',
    'G':'011/100/101/101/011', 'H':'101/101/111/101/101',
    'I':'111/010/010/010/111', 'J':'001/001/001/101/010',
    'K':'101/101/110/101/101', 'L':'100/100/100/100/111',
    'M':'101/111/111/101/101', 'N':'101/111/111/111/101',
    'O':'010/101/101/101/010', 'P':'110/101/110/100/100',
    'Q':'010/101/101/111/011', 'R':'110/101/110/101/101',
    'S':'011/100/010/001/110', 'T':'111/010/010/010/010',
    'U':'101/101/101/101/111', 'V':'101/101/101/101/010',
    'W':'101/101/111/111/101', 'X':'101/101/010/101/101',
    'Y':'101/101/010/010/010', 'Z':'111/001/010/100/111',
    '0':'111/101/101/101/111', '1':'010/110/010/010/111',
    '2':'110/001/010/100/111', '3':'110/001/010/001/110',
    '4':'101/101/111/001/001', '5':'111/100/110/001/110',
    '6':'011/100/111/101/111', '7':'111/001/010/010/010',
    '8':'111/101/111/101/111', '9':'111/101/111/001/110',
    '.':'000/000/000/000/010', '/':'001/001/010/100/100',
    ' ':'000/000/000/000/000', '?':'110/001/010/000/010',
}


def card(menu, index):
    from PIL import Image, ImageDraw
    key = animation.profile_key(menu['model'])
    image = Image.frombytes('RGBA', (72, 16), animation.frame(key, index), 'raw', 'BGRA')
    draw = ImageDraw.Draw(image)
    for e in daemon.model_menu_elements(menu, index / animation.FPS):
        if e['type'] == 'rectangle':
            if e['fill_colors'][0][-2:] == '00':
                continue
            draw.rectangle((e['x'], e['y'], e['x'] + e['width'] - 1,
                            e['y'] + e['height'] - 1), fill=e['fill_colors'][0])
        else:
            x = e['x']
            for c in e['text']:
                for y, row in enumerate(_GLYPHS.get(c, _GLYPHS['?']).split('/')):
                    for dx, bit in enumerate(row):
                        if bit == '1':
                            draw.point((x + dx, e['y'] + y + 1), fill=e['color'])
                x += daemon.est_width(c)
    confirmed = menu.get('phase') == 'confirmed'
    if menu.get('phase', 'browse') != 'saving' and index < (20 if confirmed else 8):
        image = Image.alpha_composite(image, Image.frombytes('RGBA', (72, 16),
            animation.transition_frame(key, index, menu.get('direction', 1), confirmed), 'raw', 'BGRA'))
    return image.convert('RGB')


def export(output, scale=8):
    from PIL import Image
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    models = ['gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
              'gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol',
              'gpt-5.3-codex-spark', 'gpt-5.5', 'gpt-reserve']
    menus = [dict(model=model, index=i, count=len(models), changed_at=0, until=12,
                  active=i == 1) for i, model in enumerate(models)]
    def sheet(index):
        result = Image.new('RGB', (72, len(menus) * 21 - 5), (8, 10, 15))
        for row, menu in enumerate(menus):
            result.paste(card(menu, index), (0, row * 21))
        return result.resize((result.width * scale, result.height * scale), Image.Resampling.NEAREST)
    sheet(16).save(output / 'model-cards.png')
    # Shared palette avoids color pumping between frames.
    samples = [sheet(i) for i in range(0, animation.FRAMES, 10)]
    atlas = Image.new('RGB', (samples[0].width, samples[0].height * len(samples)))
    for i, sample in enumerate(samples):
        atlas.paste(sample, (0, sample.height * i))
    palette = atlas.quantize(colors=256)
    frames = [sheet(i).quantize(palette=palette, dither=Image.Dither.NONE)
              for i in range(animation.FRAMES)]
    frames[0].save(output / 'model-cards.gif', save_all=True, append_images=frames[1:],
                   duration=40, loop=0, optimize=True, disposal=1)
    # New-generation trio, plus a temporal contact sheet for loop inspection.
    trio = [frame.crop((0, 0, 72 * scale, 58 * scale)) for frame in frames]
    trio[0].save(output / 'gpt6-models.gif', save_all=True, append_images=trio[1:],
                 duration=40, loop=0, optimize=True, disposal=1)
    phases = Image.new('RGB', (72 * 4 * scale, 58 * scale))
    for column, index in enumerate((0, 18, 37, 56)):
        phases.paste(sheet(index).crop((0, 0, 72 * scale, 58 * scale)), (column * 72 * scale, 0))
    phases.save(output / 'gpt6-phases.png')
    # A single-card walkthrough: browse -> wait for Codex -> acknowledged model.
    walkthrough = []
    for i, phase in enumerate(('browse', 'browse', 'browse', 'saving', 'confirmed')):
        menu = dict(menus[min(i, 2)], phase=phase, effort='HIGH')
        for f in range(round(CONFIRMATION_S * animation.FPS) if phase == 'confirmed' else 45):
            image = card(menu, f).resize((72 * scale, 16 * scale), Image.Resampling.NEAREST)
            walkthrough.append(image.quantize(palette=palette, dither=Image.Dither.NONE))
    walkthrough[0].save(output / 'model-picker.gif', save_all=True, append_images=walkthrough[1:],
                        duration=40, loop=0, optimize=True, disposal=1)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(export(args.out))
