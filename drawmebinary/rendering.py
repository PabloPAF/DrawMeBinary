"""
rendering.py - Re-render decoded text over the original artwork background.

Both modes first rebuild the background: every detected glyph is inpainted
away, so the artwork's shapes and colours (red circle, pink square, black
bands...) survive while the painted bits disappear.

  * basic  - decoded text centred, clean, one block per colour stream.
  * poster - each decoded character drawn at the position of the bits that
             encoded it, with random fonts and sizes.
"""
import os
import random
import time
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


def get_timestamp() -> str:
    return time.strftime('%Y%m%d_%H%M%S')


# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------
_FONT_CACHE: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
_FONT_LIST: Optional[List[str]] = None


def detect_fonts(config: Dict) -> List[str]:
    """All usable .ttf/.otf files under the configured search dirs."""
    global _FONT_LIST
    if _FONT_LIST is not None:
        return _FONT_LIST
    excl = tuple(s.lower() for s in config.get('font_exclude', ()))
    found = []
    for root in config.get('font_search_dirs', []):
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if not f.lower().endswith(('.ttf', '.otf')):
                    continue
                low = f.lower()
                if any(e in low for e in excl):
                    continue
                found.append(os.path.join(dirpath, f))
    _FONT_LIST = sorted(found)
    return _FONT_LIST


def _load_font(config: Dict, size: int,
               path: Optional[str] = None) -> ImageFont.FreeTypeFont:
    size = max(8, int(size))
    if path is None:
        pref = config.get('preferred_font', '').lower()
        fonts = detect_fonts(config)
        path = next((f for f in fonts if pref and pref in
                     os.path.basename(f).lower()), None) \
            or (fonts[0] if fonts else None)
    key = (path or 'default', size)
    if key not in _FONT_CACHE:
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, size)
        except Exception:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


# unsupported characters all map to the same .notdef glyph; comparing a
# character's rendered mask against a known-absent reference codepoint
# detects "tofu" boxes without any extra dependency
_NOTDEF_CACHE: Dict[str, bytes] = {}


def _notdef_ref(font: ImageFont.FreeTypeFont) -> bytes:
    key = getattr(font, 'path', None) or repr(font)
    if key not in _NOTDEF_CACHE:
        try:
            _NOTDEF_CACHE[key] = font.getmask('￿').tobytes()
        except Exception:
            _NOTDEF_CACHE[key] = b''
    return _NOTDEF_CACHE[key]


def _font_covers(font: ImageFont.FreeTypeFont, text: str) -> bool:
    """True if the font has a real glyph for every non-space character."""
    ref = _notdef_ref(font)
    for ch in text:
        if ch.isspace():
            continue
        try:
            if font.getmask(ch).tobytes() == ref:
                return False
        except Exception:
            return False
    return True


def _pick_font(config: Dict, size: int, text: str, fonts: List[str],
               rng) -> ImageFont.FreeTypeFont:
    """A random font that can actually render `text`; the readable default
    is the guaranteed fallback so a character is never drawn as a box."""
    tries = min(len(fonts), config.get('poster_font_tries', 12))
    for path in rng.sample(fonts, tries) if tries else []:
        font = _load_font(config, size, path)
        if _font_covers(font, text):
            return font
    default = _load_font(config, size)        # preferred / first font
    return default if _font_covers(default, text) else \
        _load_font(config, size, None)


# --------------------------------------------------------------------------
# background reconstruction
# --------------------------------------------------------------------------
def build_background(img: np.ndarray, glyphs: List[Dict],
                     config: Dict) -> np.ndarray:
    """Inpaint every detected glyph away, keeping the artwork itself."""
    if not glyphs:
        return img.copy()
    H, W = img.shape[:2]
    mask = np.zeros((H, W), np.uint8)
    for g in glyphs:
        # fill the glyph's holes (an unmasked '0' interior makes the
        # inpainting reconstruct smudges), then stamp the filled shape
        m8 = (g['mask'].astype(np.uint8)) * 255
        m8 = cv2.copyMakeBorder(m8, 1, 1, 1, 1, cv2.BORDER_CONSTANT,
                                value=0)
        flood = m8.copy()
        ff = np.zeros((m8.shape[0] + 2, m8.shape[1] + 2), np.uint8)
        cv2.floodFill(flood, ff, (0, 0), 255)
        filled = (m8 | cv2.bitwise_not(flood))[1:-1, 1:-1]
        roi = mask[g['y']:g['y'] + g['h'], g['x']:g['x'] + g['w']]
        roi |= filled[:roi.shape[0], :roi.shape[1]]
    med_h = int(np.median([g['h'] for g in glyphs]))
    it = max(config.get('inpaint_dilate_min', 3),
             med_h // config.get('inpaint_dilate_div', 4))
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=it)
    return cv2.inpaint(img, mask, config.get('inpaint_radius', 4),
                       cv2.INPAINT_TELEA)


def _bgr_to_rgb(c) -> Tuple[int, int, int]:
    return (int(c[2]), int(c[1]), int(c[0]))


def _renderable(text: str, font: ImageFont.FreeTypeFont) -> str:
    """
    Drop characters that cannot be drawn as a real glyph: control/format
    characters (a garbage decode can contain e.g. \\x99), which otherwise
    show up as '.notdef' tofu boxes. Spaces are kept. Glyph coverage for a
    chosen font is handled separately by `_pick_font` (poster mode); the
    default basic-mode font covers normal text, and stripping by coverage
    here proved too aggressive, so we only filter unprintable characters.
    """
    return ''.join(ch for ch in text if ch == ' ' or ch.isprintable())


# --------------------------------------------------------------------------
# basic mode
# --------------------------------------------------------------------------
def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int,
          draw: ImageDraw.ImageDraw) -> List[str]:
    out = []
    for raw in text.split('\n'):
        words, line = raw.split(' '), ''
        for w in words:
            trial = (line + ' ' + w).strip()
            if draw.textlength(trial, font=font) <= max_w or not line:
                line = trial
            else:
                out.append(line)
                line = w
        out.append(line)
    return [l for l in out if l != ''] or ['']


def _fit_font(text: str, config: Dict, max_w: int, max_h: int,
              draw: ImageDraw.ImageDraw) -> Tuple[ImageFont.FreeTypeFont,
                                                  List[str]]:
    lo = config.get('basic_min_font_pt', 15)
    hi = config.get('basic_max_font_pt', 110)
    spacing = config.get('basic_line_spacing', 1.25)
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _load_font(config, mid)
        lines = _wrap(text, font, max_w, draw)
        h = len(lines) * mid * spacing
        w = max(draw.textlength(l, font=font) for l in lines)
        if h <= max_h and w <= max_w:
            best = (font, lines, mid)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        font = _load_font(config, config.get('basic_min_font_pt', 15))
        return font, _wrap(text, font, max_w, draw)
    return best[0], best[1]


def _save(pil: Image.Image, config: Dict, image_path: str,
          suffix: str, orig_size=None) -> str:
    from security import safe_basename, safe_output_path
    if orig_size and tuple(orig_size) != pil.size:
        pil = pil.resize(orig_size, Image.LANCZOS)
    out_dir = config.get('output_dir', 'output')
    os.makedirs(out_dir, exist_ok=True)
    base = safe_basename(image_path or 'image')
    path = safe_output_path(
        out_dir, f'{base}_{suffix}_{get_timestamp()}.png')
    pil.save(path)
    print(f'   Saved: {path}')
    return path


def _render_stream_positioned(draw: ImageDraw.ImageDraw, s: Dict,
                              config: Dict) -> None:
    """
    Draw one stream's decoded text where its bits were painted: every
    decoded character sits at the centre of the 0/1 group that encoded it
    (caption words at the centre of their original word). Font size comes
    from the painted digit height, uniform within a line.
    """
    from decoding import units_to_lines
    color = _bgr_to_rgb(s.get('color', (0, 0, 0)))
    spacing = config.get('basic_line_spacing', 1.25)
    fmin = config.get('basic_min_font_pt', 15)
    fmax = config.get('basic_max_font_pt', 110)
    hmult = config.get('basic_position_height_mult', 1.5)
    lines = units_to_lines(s.get('units', []), config)

    for line in lines:
        if not line['text'].strip():
            continue
        x, y, w, h = line['bbox']
        if '\n' in line['text']:      # flat-fallback unit: wrap in bbox
            font, rows = _fit_font(line['text'], config, max(w, 50),
                                   max(h, 30), draw)
            ty = y
            for row in rows:
                row = _renderable(row, font)
                if row.strip():
                    draw.text((x, ty), row, font=font, fill=color)
                ty += int(getattr(font, 'size', 12) * spacing)
            continue
        # uniform size per line, from the height of the painted digits
        # (not the line bbox: a nibble pair spans two digit rows)
        glyph_hs = sorted(g['h'] for u in line.get('units', [])
                          for g in u.get('glyphs', [])) or [h]
        gh = glyph_hs[len(glyph_hs) // 2]
        size = int(min(fmax, max(fmin, gh * hmult)))
        font = _load_font(config, size)
        for u in line.get('units', []):
            txt = _renderable(u['text'].strip(), font)
            if not txt:
                continue
            ucolor = _bgr_to_rgb(u['color']) if u.get('color') else color
            ux, uy, uw, uh = u['bbox']
            usize, ufont = size, font
            while usize > fmin and \
                    draw.textlength(txt, font=ufont) > max(uw * 1.6, 30):
                usize -= 2
                ufont = _load_font(config, usize)
            tw = draw.textlength(txt, font=ufont)
            draw.text((ux + (uw - tw) / 2, uy + (uh - usize) / 2),
                      txt, font=ufont, fill=ucolor)


def render_basic_mode(data: Dict, config: Dict,
                      image_path: str = '',
                      img: Optional[np.ndarray] = None) -> str:
    """
    Clean re-render of all decoded streams over the original background.
    By default ('basic_keep_positions': True) each decoded line is drawn
    where its source bits were painted, in the stream's ink colour; set
    the flag to False for the centred layout instead.
    """
    if img is None:
        img = cv2.imread(image_path, cv2.IMREAD_COLOR)
    bg = build_background(img, data.get('all_bits', []), config)
    pil = Image.fromarray(cv2.cvtColor(bg, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    W, H = pil.size
    margin = int(min(W, H) * config.get('basic_margin_frac', 0.10))
    max_w = W - 2 * margin
    spacing = config.get('basic_line_spacing', 1.25)

    streams = [s for s in data.get('streams', []) if s.get('text')]
    if not streams:
        return _save(pil, config, image_path, 'basic',
                 data.get('orig_size'))

    if config.get('basic_keep_positions', True):
        for s in streams:
            _render_stream_positioned(draw, s, config)
        return _save(pil, config, image_path, 'basic',
                     data.get('orig_size'))

    # centred layout: fit each stream, centre the whole stack vertically
    blocks = []
    budget = (H - 2 * margin) / max(1, len(streams))
    for s in streams:
        font, lines = _fit_font(s['text'], config, max_w,
                                int(budget * 0.9), draw)
        size = font.size if hasattr(font, 'size') else 12
        blocks.append({'font': font, 'lines': lines, 'size': size,
                       'color': _bgr_to_rgb(s.get('color', (0, 0, 0))),
                       'h': int(len(lines) * size * spacing)})
    total_h = sum(b['h'] for b in blocks) + \
        int(0.5 * blocks[0]['size'] * (len(blocks) - 1))
    y = max(margin, (H - total_h) // 2)
    for b in blocks:
        for line in b['lines']:
            line = _renderable(line, b['font'])
            lw = draw.textlength(line, font=b['font'])
            draw.text(((W - lw) / 2, y), line, font=b['font'],
                      fill=b['color'])
            y += int(b['size'] * spacing)
        y += int(0.5 * b['size'])
    return _save(pil, config, image_path, 'basic',
                 data.get('orig_size'))


# --------------------------------------------------------------------------
# poster mode
# --------------------------------------------------------------------------
def render_poster_mode(data: Dict, config: Dict,
                       validator=None, image_path: str = '',
                       img: Optional[np.ndarray] = None) -> str:
    """Scatter each decoded character at the position of its source bits."""
    if img is None:
        img = cv2.imread(image_path, cv2.IMREAD_COLOR)
    bg = build_background(img, data.get('all_bits', []), config)
    pil = Image.fromarray(cv2.cvtColor(bg, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    W, H = pil.size
    fonts = detect_fonts(config)
    rng = random.Random(config.get('poster_seed', 42))

    mult = config.get('poster_bbox_multiplier', 0.9)
    var = config.get('poster_font_variance', 0.6)
    jit = config.get('poster_jitter', 0.25)
    fmin = config.get('poster_min_font_pt', 18)
    fmax = config.get('poster_max_font_pt', 130)

    for s in data.get('streams', []):
        color = _bgr_to_rgb(s.get('color', (0, 0, 0)))
        for u in s.get('units', []):
            txt = u['text']
            if not txt.strip():
                continue
            ucolor = _bgr_to_rgb(u['color']) if u.get('color') else color
            x, y, w, h = u['bbox']
            # a unit's source bits often span far wider than tall (an 8-bit
            # group); let width contribute so poster glyphs stay visible,
            # but never beyond ~2 line heights to avoid pile-ups
            base = max(h, min(
                config.get('poster_width_size_frac', 0.3) * w /
                max(1, len(txt)),
                config.get('poster_max_rel_height', 2.2) * h))
            size = base * mult * (1 + rng.uniform(-var, var))
            size = int(max(fmin, min(fmax, size)))
            if fonts and config.get('poster_use_random_fonts', True):
                font = _pick_font(config, size, txt, fonts, rng)
            else:
                font = _load_font(config, size)
            # drop control chars / glyphs the chosen font lacks (no tofu)
            txt = _renderable(txt, font)
            if not txt.strip():
                continue
            # scatter near (not far from) the original spot
            cx = x + w / 2 + rng.uniform(-jit, jit) * max(h, w / 4)
            cy = y + h / 2 + rng.uniform(-jit, jit) * h * 1.5
            # random boldness via stroke width
            stroke = rng.randint(0, config.get('poster_bold_max', 2))
            stroke = int(stroke * size / 40)
            tw = draw.textlength(txt, font=font)
            # keep the whole glyph on-canvas (no overflow off the edges)
            px = min(max(cx - tw / 2, 0), max(0, W - tw))
            py = min(max(cy - size / 2, 0), max(0, H - size))
            draw.text((px, py), txt, font=font, fill=ucolor,
                      stroke_width=stroke, stroke_fill=ucolor)
    return _save(pil, config, image_path, 'poster',
                 data.get('orig_size'))
