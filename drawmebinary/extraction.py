"""
extraction.py - Locate and classify binary glyphs in artwork.

Pipeline:
  1. Adaptive threshold in both polarities (dark-on-light AND light-on-dark)
     so marks are found on any background colour.
  2. Connected components -> candidate glyphs, filtered by size/shape and by
     structure (a real bit always sits in a line with neighbours).
  3. Classification, three layers:
       a. one word-level Tesseract pass over a clean re-rendered canvas
          (background removed). Words made of 0/1 map to bits; words made of
          letters become 'txt' passthrough characters (artwork captions),
       b. per-glyph shape features (enclosed hole, aspect ratio, hollow
          centre) for glyphs OCR missed,
       c. the optional keras MNIST verifier when tensorflow is installed.
  4. Cluster ink colours into streams (each colour = independent text stream).

A glyph dict:
  {'bit': '0'|'1'|None, 'kind': 'bin'|'txt', 'char': str, 'x','y','w','h',
   'cx','cy', 'color': (b,g,r), 'mask': bool ndarray, 'conf': float,
   'stream': int}
"""
import os
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from config import CONFIG

try:
    import pytesseract
    _HAS_TESS = True
except ImportError:                                   # pragma: no cover
    _HAS_TESS = False

_KERAS = None          # None = not tried, False = unavailable, else model

_ZERO_LOOKALIKES = set('0OoQDØ689')   # common OCR misreads of printed 0
_ONE_LOOKALIKES = set("1Il|!/\\ij7")  # common OCR misreads of printed 1
_PUNCT_KEEP = set(".,'!?-:;\"()")


# --------------------------------------------------------------------------
# image loading
# --------------------------------------------------------------------------
def load_image(path: str) -> np.ndarray:
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f'Cannot read image: {path}')
    return img


# --------------------------------------------------------------------------
# keras verifier (optional)
# --------------------------------------------------------------------------
def _get_keras(config: Dict):
    global _KERAS
    if _KERAS is not None:
        return _KERAS or None
    _KERAS = False
    if not config.get('use_keras', True):
        return None
    path = config.get('keras_model_path', '')
    if not path or not os.path.exists(path):
        return None
    try:                                              # pragma: no cover
        from tensorflow import keras
        _KERAS = keras.models.load_model(path)
    except Exception:
        _KERAS = False
    return _KERAS or None


def _keras_predict(mask: np.ndarray, config: Dict) -> Optional[str]:
    """MNIST-style 28x28 prediction. Returns '0'/'1' or None."""
    model = _get_keras(config)
    if model is None:
        return None
    h, w = mask.shape
    side = max(h, w)
    pad = np.zeros((side + 8, side + 8), np.float32)
    y0 = (pad.shape[0] - h) // 2
    x0 = (pad.shape[1] - w) // 2
    pad[y0:y0 + h, x0:x0 + w] = mask.astype(np.float32)
    img28 = cv2.resize(pad, (28, 28), interpolation=cv2.INTER_AREA)
    try:                                              # pragma: no cover
        p = float(model.predict(img28[None, ...], verbose=0)[0][0])
    except Exception:
        return None
    if p > 0.75:
        return '1'
    if p < 0.25:
        return '0'
    return None


# --------------------------------------------------------------------------
# shape classification
# --------------------------------------------------------------------------
def _count_holes(mask_u8: np.ndarray) -> int:
    cnts, hier = cv2.findContours(mask_u8, cv2.RETR_CCOMP,
                                  cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return 0
    h, w = mask_u8.shape
    min_area = max(2.0, 0.02 * h * w)
    holes = 0
    for i in range(len(cnts)):
        if hier[0][i][3] != -1 and cv2.contourArea(cnts[i]) >= min_area:
            holes += 1
    return holes


def classify_shape(mask: np.ndarray,
                   config: Optional[Dict] = None
                   ) -> Tuple[Optional[str], float]:
    """
    Cheap 0/1 classifier from glyph geometry.
    Returns (bit, confidence) or (None, 0) when ambiguous.
    NOTE: only trustworthy when the image contains digits, which is why the
    word-level OCR pass gets priority for everything that looks like prose.
    """
    cfg = config or CONFIG
    h, w = mask.shape
    if h < 3 or w < 1:
        return None, 0.0
    aspect = w / h
    fill = float(mask.mean())
    m8 = (mask.astype(np.uint8)) * 255
    m8 = cv2.copyMakeBorder(m8, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=0)
    holes = _count_holes(m8)

    if holes >= 1 and aspect >= cfg['shape_hole_min_aspect']:
        return '0', 0.85                      # ring with a hole
    if holes == 0:
        if aspect <= cfg['shape_one_max_aspect']:
            return '1', 0.8                   # tall narrow stroke
        ch0, ch1 = h // 3, max(h // 3 + 1, 2 * h // 3)
        cw0, cw1 = w // 3, max(w // 3 + 1, 2 * w // 3)
        centre = float(mask[ch0:ch1, cw0:cw1].mean())
        if aspect >= cfg['shape_hollow_min_aspect'] and \
                fill > cfg['shape_hollow_min_fill'] and \
                centre < cfg['shape_hollow_centre_frac'] * max(fill,
                                                               1e-6):
            return '0', 0.55                  # broken / hand-painted ring
        if aspect <= cfg['shape_bold_one_max_aspect'] and \
                fill >= cfg['shape_bold_one_min_fill']:
            return '1', 0.55                  # bold 1 with serif/base
        # '1' with flag and base serif: stem much narrower than the bbox
        if aspect <= cfg['shape_stem_max_aspect']:
            rows = mask[h // 3: 2 * h // 3]
            if rows.size:
                widths = rows.sum(axis=1)
                if float(widths.mean()) <= \
                        cfg['shape_stem_width_frac'] * w:
                    return '1', 0.6
        # abstract block art: a solid square block reads as 0, a solid
        # narrow bar as 1 (the bar case is covered by the aspect rule)
        blo, bhi = cfg['shape_block_aspect']
        if blo <= aspect <= bhi and fill >= cfg['shape_block_min_fill']:
            return '0', 0.5
    return None, 0.0


# --------------------------------------------------------------------------
# word + character level OCR pass
# --------------------------------------------------------------------------
def _render_clean_canvas(cands: List[Dict], shape: Tuple[int, int],
                         scale: int = 2) -> np.ndarray:
    """All candidate glyphs re-rendered black-on-white (no background)."""
    H, W = shape
    canvas = np.full((H * scale, W * scale), 255, np.uint8)
    for c in cands:
        m = c['mask'].astype(np.uint8) * 255
        m = cv2.resize(m, (c['w'] * scale, c['h'] * scale),
                       interpolation=cv2.INTER_NEAREST)
        y, x = c['y'] * scale, c['x'] * scale
        roi = canvas[y:y + m.shape[0], x:x + m.shape[1]]
        roi[m > 0] = 0
    return canvas


def _ocr_pass(cands: List[Dict], shape: Tuple[int, int],
              whitelist: str = '') -> Tuple[List[Dict], List[Dict]]:
    """
    One Tesseract pass over the clean canvas.
    Returns (words, charboxes); words from image_to_data (with confidence),
    charboxes from image_to_boxes (per-character bounding boxes, which stay
    correct even when connected components split a serif '0' into arcs or
    merge touching digits). An optional character whitelist makes the
    digits-only pass far more reliable on long 0/1 strings.
    """
    if not _HAS_TESS or not cands:
        return [], []
    med_h = sorted(c['h'] for c in cands)[len(cands) // 2]
    s_lo, s_hi = CONFIG['ocr_scale_range']
    scale = int(max(s_lo, min(s_hi, round(
        CONFIG['ocr_target_glyph_px'] / max(1, med_h)))))
    cfg = f"--psm {CONFIG['ocr_psm']}"
    if whitelist:
        cfg += f' -c tessedit_char_whitelist={whitelist}'
    canvas = _render_clean_canvas(cands, shape, scale)
    Hc = canvas.shape[0]
    words, boxes = [], []
    try:
        data = pytesseract.image_to_data(
            canvas, config=cfg, output_type=pytesseract.Output.DICT)
        for i in range(len(data['text'])):
            t = (data['text'][i] or '').strip()
            if not t:
                continue
            words.append({'text': t,
                          'x': data['left'][i] / scale,
                          'y': data['top'][i] / scale,
                          'w': data['width'][i] / scale,
                          'h': data['height'][i] / scale,
                          'conf': float(data['conf'][i])})
        raw = pytesseract.image_to_boxes(canvas, config=cfg)
        for line in raw.splitlines():
            p = line.split()
            if len(p) < 5:
                continue
            ch, x0, y0, x1, y1 = p[0], int(p[1]), int(p[2]), int(p[3]), \
                int(p[4])
            # image_to_boxes uses a bottom-left origin
            boxes.append({'char': ch,
                          'x': x0 / scale, 'y': (Hc - y1) / scale,
                          'w': (x1 - x0) / scale, 'h': (y1 - y0) / scale})
    except Exception:                                  # pragma: no cover
        return words, []
    return words, boxes


def _word_is_binary(text: str, config: Optional[Dict] = None) -> bool:
    """Mostly 0/1 lookalikes, with at least one strict 0/1 character."""
    cfg = config or CONFIG
    look = sum(1 for c in text
               if c in _ZERO_LOOKALIKES or c in _ONE_LOOKALIKES or c in '01')
    strict = sum(1 for c in text if c in '01')
    return look >= cfg['binary_word_look_frac'] * len(text) and \
        strict >= max(1, cfg['binary_word_strict_frac'] * len(text))


_VALIDATOR = None


def _caption_variants(text: str) -> List[str]:
    """Lookalike-digit normalisations of an OCR word ('stup1' -> 'stupi',
    'stupl'; '1t' -> 'it', 'lt')."""
    outs = {text}
    for digit, repls in (('1', 'il'), ('0', 'o'), ('5', 's'), ('8', 'b')):
        for cur in list(outs):
            if digit in cur:
                for r in repls:
                    outs.add(cur.replace(digit, r))
    return [o for o in outs if o.isalpha()]


def _is_caption_word(word: Dict) -> bool:
    """
    A caption word is a real word, not a digit string Tesseract dressed up
    in letters ('01011001' -> 'QLOLL'). Require a (fuzzy) dictionary hit -
    OCR misreads letters in artwork ('rutned' for 'ruined') - or very high
    OCR confidence for names/unknown words.
    """
    global _VALIDATOR
    if _word_is_binary(word['text']):
        return False
    text = word['text'].strip(".,!?:;\"'()")
    variants = _caption_variants(text.lower())
    if not text or not variants:
        return False
    if word['conf'] >= CONFIG['ocr_caption_trust_conf'] and \
            text.isalpha():
        return True
    if _VALIDATOR is None:
        from decoding import LanguageValidator
        _VALIDATOR = LanguageValidator()
    return any(_VALIDATOR.word_plausible(v) for v in variants)


def _boxes_to_glyphs(ink: np.ndarray, cands: List[Dict], words: List[Dict],
                     boxes: List[Dict], config: Dict, mode: str,
                     claimed: Optional[np.ndarray] = None) -> List[Dict]:
    """
    Build glyphs by cutting the ink mask with OCR character boxes - this
    stays correct even when touching digits form one connected component.
    mode='txt' keeps only characters of alphabetic (caption) words;
    mode='bin' keeps only 0/1 (digits-only whitelist pass).
    Components mostly covered by accepted boxes are marked 'consumed';
    the rest fall through to the shape classifier.
    """
    def overlap(b, w):
        ix = max(0.0, min(b['x'] + b['w'], w['x'] + w['w'])
                 - max(b['x'], w['x']))
        iy = max(0.0, min(b['y'] + b['h'], w['y'] + w['h'])
                 - max(b['y'], w['y']))
        return ix * iy / max(1.0, b['w'] * b['h'])

    H, W = ink.shape
    painted = claimed if claimed is not None else np.zeros((H, W), bool)
    glyphs: List[Dict] = []
    # Tesseract reports conf 0 for long whitelisted digit strings even when
    # every character is right - only the caption (txt) pass needs the gate.
    min_conf = config.get('ocr_word_min_conf', 60) if mode == 'txt' else 0
    for box in boxes:
        word = max(words, key=lambda w: overlap(box, w), default=None)
        if word is None or \
                overlap(box, word) < config.get('ocr_box_overlap', 0.5) \
                or word['conf'] < min_conf:
            continue
        ch = box['char']
        if mode == 'bin':
            if ch not in '01':
                continue
            kind, bit, char, conf = 'bin', ch, ch, 0.85
        else:
            if not _is_caption_word(word):
                continue
            # inside a real word, lookalike digits are misread letters
            ch = {'1': 'i', '0': 'o', '5': 's', '8': 'b'}.get(ch, ch)
            if not (ch.isalpha() or ch in _PUNCT_KEEP):
                continue
            kind, bit, char, conf = 'txt', None, ch, 0.6
        x0 = max(0, int(box['x']) - 1)
        y0 = max(0, int(box['y']) - 1)
        x1 = min(W, int(box['x'] + box['w']) + 2)
        y1 = min(H, int(box['y'] + box['h']) + 2)
        if x1 <= x0 or y1 <= y0:
            continue
        region = ink[y0:y1, x0:x1] > 0
        region &= ~painted[y0:y1, x0:x1]      # don't double-claim ink
        area = int(region.sum())
        if area < config.get('min_glyph_area', 8):
            continue
        ys, xs = np.nonzero(region)
        gx0, gx1 = x0 + xs.min(), x0 + xs.max() + 1
        gy0, gy1 = y0 + ys.min(), y0 + ys.max() + 1
        mask = region[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        painted[y0:y1, x0:x1] |= region
        glyphs.append({'x': int(gx0), 'y': int(gy0),
                       'w': int(gx1 - gx0), 'h': int(gy1 - gy0),
                       'cx': (gx0 + gx1) / 2.0, 'cy': (gy0 + gy1) / 2.0,
                       'area': area, 'fill': float(mask.mean()),
                       'mask': mask, 'kind': kind, 'bit': bit,
                       'char': char, 'conf': conf})

    # mark components whose ink is mostly claimed by boxes
    for c in cands:
        if c.get('kind') is not None:
            continue
        sub = painted[c['y']:c['y'] + c['h'], c['x']:c['x'] + c['w']]
        cov = float((sub & c['mask']).sum()) / max(1, c['area'])
        if cov > 0.5:
            c['kind'] = 'consumed'
    return glyphs


# --------------------------------------------------------------------------
# candidate detection
# --------------------------------------------------------------------------
def _ink_mask(img: np.ndarray, config: Dict) -> np.ndarray:
    """
    Ink = pixels that differ clearly from the local background, estimated
    with a colour median blur. One pass, works for any ink/background
    combination (black-on-white, white-on-red, ...), and keeps thin
    anti-aliased strokes intact (unlike adaptive thresholding, which splits
    small serif zeros into arcs and fires on hole interiors).
    """
    H, W = img.shape[:2]
    k = max(31, (min(H, W) // config.get('bg_kernel_frac', 8)) | 1)
    k = min(k, 99)
    bg = cv2.medianBlur(img, k)
    diff = np.linalg.norm(img.astype(np.int16) - bg.astype(np.int16),
                          axis=2)
    # no morphological opening: it destroys thin serif strokes; isolated
    # noise pixels are removed by the component area filter instead
    return (diff > config.get('ink_threshold', 40)).astype(np.uint8) * 255


def _dominant_colors(img: np.ndarray, frac: float = 0.05) -> np.ndarray:
    """Centroids of the image's dominant (background) colours, from a
    coarse 512-bin colour histogram."""
    q = (img >> 5).astype(np.int32)
    flat = ((q[:, :, 0] << 6) | (q[:, :, 1] << 3) | q[:, :, 2]).ravel()
    counts = np.bincount(flat, minlength=512)
    centers = []
    px = img.reshape(-1, 3)
    for b in np.nonzero(counts > frac * flat.size)[0]:
        centers.append(px[flat == b].mean(axis=0))
    return np.array(centers, np.float32) if centers else \
        np.zeros((0, 3), np.float32)


def _near_dominant(region: np.ndarray, centers: np.ndarray,
                   tol: float = 70.0) -> np.ndarray:
    """True where a pixel is within tol of any dominant colour - includes
    the anti-aliased blend pixels along shape boundaries."""
    if len(centers) == 0:
        return np.zeros(region.shape[:2], bool)
    px = region.reshape(-1, 1, 3).astype(np.float32)
    d = np.linalg.norm(px - centers[None, :, :], axis=2).min(axis=1)
    return (d < tol).reshape(region.shape[:2])


def _components(img: np.ndarray, config: Dict,
                ink: Optional[np.ndarray] = None) -> List[Dict]:
    H, W = img.shape[:2]
    bw = ink if ink is not None else _ink_mask(img, config)
    cands = []
    dom = None

    def consider(x, y, w, h, area, mask, salvage_ok):
        nonlocal dom
        oversized = (w > h * config.get('max_run_aspect', 8.0) or
                     h > H * config['max_glyph_frac'] or
                     w * h > (H * W) // config.get('oversize_area_div',
                                                   50))
        if oversized and salvage_ok and \
                area > config.get('salvage_area_factor', 4) * \
                config['min_glyph_area']:
            # A glyph drawn across a colour boundary merges with the thin
            # 'edge band' the background model produces there. Remove
            # pixels near dominant (background) colours and re-extract.
            if dom is None:
                dom = _dominant_colors(
                    img, config.get('dominant_color_frac', 0.05))
            sub = mask.copy()
            sub[_near_dominant(img[y:y + h, x:x + w], dom,
                               config.get('dominant_color_tol',
                                          70.0))] = False
            n2, lab2, st2, ce2 = cv2.connectedComponentsWithStats(
                sub.astype(np.uint8), 8)
            for j in range(1, n2):
                x2, y2, w2, h2, a2 = st2[j]
                consider(x + x2, y + y2, w2, h2, a2,
                         lab2[y2:y2 + h2, x2:x2 + w2] == j, False)
            return
        if (oversized or
                h < config['min_glyph_h'] or
                area < config['min_glyph_area']):
            return
        fill = area / float(w * h)
        if not (config['min_fill'] <= fill <= config['max_fill']):
            return
        cands.append({'x': int(x), 'y': int(y), 'w': int(w), 'h': int(h),
                      'cx': x + w / 2.0, 'cy': y + h / 2.0,
                      'area': int(area), 'fill': fill, 'mask': mask,
                      'kind': None, 'bit': None, 'char': None,
                      'conf': 0.0})

    n, labels, stats, _cents = cv2.connectedComponentsWithStats(bw, 8)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        consider(x, y, w, h, area, labels[y:y + h, x:x + w] == i, True)
    return _dedupe(cands)


def _dedupe(cands: List[Dict]) -> List[Dict]:
    """Both polarities can fire on the same mark; keep one per location."""
    cands = sorted(cands, key=lambda c: -c['area'])
    kept: List[Dict] = []
    for c in cands:
        dup = False
        for k in kept:
            ix = max(0, min(c['x'] + c['w'], k['x'] + k['w'])
                     - max(c['x'], k['x']))
            iy = max(0, min(c['y'] + c['h'], k['y'] + k['h'])
                     - max(c['y'], k['y']))
            if ix * iy > 0.5 * min(c['w'] * c['h'], k['w'] * k['h']):
                dup = True
                break
        if not dup:
            kept.append(c)
    return kept


def _contrast_filter(img: np.ndarray, cands: List[Dict],
                     config: Optional[Dict] = None) -> List[Dict]:
    """
    Drop 'halo' components: adaptive thresholding fires on background pixels
    that surround real glyphs (e.g. red around white text). A real glyph's
    ink colour differs clearly from the colour just outside its bbox.
    """
    cfg = config or CONFIG
    min_contrast = cfg.get('min_contrast', 30.0)
    m = cfg.get('contrast_ring_px', 3)
    H, W = img.shape[:2]
    kept = []
    for c in cands:
        ink_px = img[c['y']:c['y'] + c['h'],
                     c['x']:c['x'] + c['w']][c['mask']]
        if len(ink_px) == 0:
            continue
        ink = np.median(ink_px, axis=0)
        x0, y0 = max(0, c['x'] - m), max(0, c['y'] - m)
        x1, y1 = min(W, c['x'] + c['w'] + m), min(H, c['y'] + c['h'] + m)
        ring = np.zeros((y1 - y0, x1 - x0), bool)
        ring[:, :] = True
        iy0, ix0 = c['y'] - y0, c['x'] - x0
        ring[iy0:iy0 + c['h'], ix0:ix0 + c['w']] = False
        ring_px = img[y0:y1, x0:x1][ring]
        if len(ring_px) == 0:
            continue
        surround = np.median(ring_px, axis=0)
        if float(np.linalg.norm(ink - surround)) >= min_contrast:
            kept.append(c)
    return kept


def _height_groups(cands: List[Dict], config: Dict) -> List[Dict]:
    """Keep only glyphs belonging to a consistent text-size group."""
    if not cands:
        return []
    cands = sorted(cands, key=lambda c: c['h'])
    groups, cur = [], [cands[0]]
    for c in cands[1:]:
        if c['h'] <= cur[-1]['h'] * config['height_group_ratio']:
            cur.append(c)
        else:
            groups.append(cur)
            cur = [c]
    groups.append(cur)
    keep = []
    for grp in groups:
        if len(grp) >= config['min_group_size']:
            keep.extend(grp)
    return keep


def _line_structure_filter(cands: List[Dict], config: Dict) -> List[Dict]:
    """A real glyph has aligned neighbours of similar size in its line."""
    dy = config.get('neighbor_dy_frac', 0.7)
    rlo, rhi = config.get('neighbor_h_ratio', (0.5, 2.0))
    dx = config.get('neighbor_max_dx_h', 30)
    need = config.get('neighbor_min_count', 2)
    keep = []
    for c in cands:
        n = 0
        for o in cands:
            if o is c:
                continue
            if (abs(o['cy'] - c['cy']) < dy * c['h'] and
                    rlo < o['h'] / c['h'] < rhi and
                    abs(o['cx'] - c['cx']) < dx * c['h']):
                n += 1
                if n >= need:
                    break
        if n >= need:
            keep.append(c)
    return keep


def _tess_run_bits(c: Dict, config: Optional[Dict] = None
                   ) -> Optional[str]:
    """OCR a multi-digit blob as a 0/1 string (digits whitelist, one line)."""
    cfg = config or CONFIG
    if not _HAS_TESS:
        return None
    m = (c['mask'].astype(np.uint8)) * 255
    scale = max(2, int(round(cfg['ocr_run_target_px'] / max(1, c['h']))))
    m = cv2.resize(m, (c['w'] * scale, c['h'] * scale),
                   interpolation=cv2.INTER_NEAREST)
    m = cv2.copyMakeBorder(m, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=0)
    m = cv2.bitwise_not(m)
    try:
        s = pytesseract.image_to_string(
            m, config=f"--psm {cfg['ocr_run_psm']} "
                      "-c tessedit_char_whitelist=01").strip()
    except Exception:                                  # pragma: no cover
        return None
    s = s.replace(' ', '')
    return s if s and set(s) <= {'0', '1'} else None


def _split_wide(cands: List[Dict], config: Dict) -> List[Dict]:
    """
    Split components that clearly span several touching digits (e.g. '00'
    merging into one blob at small font sizes). OCR (digits whitelist) is
    tried first; ink-density minima splitting is the geometric fallback.
    """
    widths = sorted(c['w'] for c in cands)
    med_w = widths[len(widths) // 2] if widths else 0
    out = []
    for c in cands:
        ref = med_w if med_w else 0.8 * c['h']
        if c.get('kind') is not None or \
                c['w'] <= config.get('split_trigger_ratio', 1.2) * ref:
            out.append(c)
            continue
        n_hi = max(2, int(np.ceil(
            c['w'] / (config.get('split_min_piece_frac', 0.7) * ref))))
        ocr_bits = _tess_run_bits(c, config) \
            if c['w'] > config.get('split_ocr_ratio', 1.6) * ref else None

        def piece_score(pieces, cut_cost):
            confs = []
            for p in pieces:
                bit, conf = classify_shape(p['mask'], config)
                p['bit'], p['char'], p['conf'] = bit, bit, conf
                p['kind'] = 'bin' if bit is not None else None
                confs.append(conf)
            score = float(np.mean(confs))
            # prefer glyph-sized pieces ('00' must become two digits, a
            # lone '0' must stay one) and cuts through empty columns
            dev = float(np.mean([abs(p['w'] - ref) for p in pieces])) / ref
            return score - \
                config.get('split_width_dev_penalty', 0.3) * min(1.0, dev) \
                - config.get('split_cut_cost_penalty', 0.5) * cut_cost

        single = dict(c)
        best, best_score = [single], piece_score([single], 0.0)
        for n in range(2, n_hi + 1):
            cut = _cut_blob(c, n, config)
            if cut is None:
                continue
            pieces, cost = cut
            score = piece_score(pieces, cost)
            if ocr_bits and len(ocr_bits) == n:
                score += config.get('split_ocr_bonus', 0.2)
                for p, b in zip(pieces, ocr_bits):   # OCR breaks ties
                    if p['bit'] is None:
                        p.update(kind='bin', bit=b, char=b, conf=0.5)
            if score > best_score:
                best, best_score = pieces, score
        if best_score >= config.get('split_accept_score', 0.25):
            out.extend(best)
        else:
            out.append(c)
    return out


def _cut_blob(c: Dict, n: int,
              config: Dict) -> Optional[Tuple[List[Dict], float]]:
    """Cut a blob into n pieces at ink-density minima near equal steps.
    Returns (pieces, cut_cost); cost is the ink crossed by the cuts,
    relative to glyph height (cutting a '0' in half is expensive,
    separating touching digits is nearly free)."""
    cols = c['mask'].sum(axis=0).astype(float)
    step = c['w'] / n
    cuts = [0]
    for i in range(1, n):
        centre = int(round(i * step))
        lo = max(cuts[-1] + 2, centre - int(step / 3) - 1)
        hi = min(c['w'] - 2, centre + int(step / 3) + 1)
        if hi <= lo:
            return None
        cuts.append(lo + int(np.argmin(cols[lo:hi])))
    cuts.append(c['w'])
    cost = float(np.mean([cols[k] for k in cuts[1:-1]])) / max(1, c['h'])
    pieces = []
    for a, b in zip(cuts, cuts[1:]):
        sub = c['mask'][:, a:b]
        ys, xs = np.nonzero(sub)
        if len(xs) < config.get('min_glyph_area', 8):
            continue
        m = sub[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        x0 = c['x'] + a + int(xs.min())
        y0 = c['y'] + int(ys.min())
        pieces.append({'x': x0, 'y': y0, 'w': m.shape[1], 'h': m.shape[0],
                       'cx': x0 + m.shape[1] / 2.0,
                       'cy': y0 + m.shape[0] / 2.0,
                       'area': int(m.sum()), 'fill': float(m.mean()),
                       'mask': m, 'kind': None, 'bit': None,
                       'char': None, 'conf': 0.0})
    if not pieces:
        return None
    return pieces, cost


# --------------------------------------------------------------------------
# colour streams
# --------------------------------------------------------------------------
def _ink_color(img: np.ndarray, g: Dict) -> Tuple[int, int, int]:
    """
    Median colour of the glyph's CORE pixels - the half of the ink farthest
    from the surrounding colour. Plain medians drift towards the background
    on thin anti-aliased strokes, which would split one text into several
    colour streams.
    """
    H, W = img.shape[:2]
    crop = img[g['y']:g['y'] + g['h'], g['x']:g['x'] + g['w']]
    px = crop[g['mask']].astype(np.float32)
    if len(px) == 0:
        return (0, 0, 0)
    m = 3
    x0, y0 = max(0, g['x'] - m), max(0, g['y'] - m)
    x1, y1 = min(W, g['x'] + g['w'] + m), min(H, g['y'] + g['h'] + m)
    ring = np.ones((y1 - y0, x1 - x0), bool)
    ring[g['y'] - y0:g['y'] - y0 + g['h'],
         g['x'] - x0:g['x'] - x0 + g['w']] = False
    ring_px = img[y0:y1, x0:x1][ring]
    if len(ring_px):
        surround = np.median(ring_px, axis=0).astype(np.float32)
        d = np.linalg.norm(px - surround, axis=1)
        core = px[d >= np.median(d)]
        if len(core):
            px = core
    med = np.median(px, axis=0)
    return tuple(int(v) for v in med)


def cluster_streams(glyphs: List[Dict], config: Dict) -> List[Dict]:
    """Assign a stream id per ink colour. Tiny clusters merge into nearest."""
    tol = config.get('color_tolerance', 45)
    centers: List[np.ndarray] = []
    members: List[List[Dict]] = []
    for g in glyphs:
        col = np.array(g['color'], float)
        best, bd = -1, 1e9
        for i, c in enumerate(centers):
            d = float(np.linalg.norm(col - c))
            if d < bd:
                best, bd = i, d
        if best >= 0 and bd <= tol:
            members[best].append(g)
            n = len(members[best])
            centers[best] = centers[best] * (n - 1) / n + col / n
        else:
            centers.append(col)
            members.append([g])
    min_n = config.get('min_stream_glyphs', 4)
    big = [i for i in range(len(centers)) if len(members[i]) >= min_n]
    mapping, sid = {}, 0
    for i in range(len(centers)):
        if i in big or not big:
            mapping[i] = sid
            sid += 1
    for i in range(len(centers)):
        if i not in mapping:
            j = min(big, key=lambda b: np.linalg.norm(centers[i] - centers[b]))
            d = float(np.linalg.norm(centers[i] - centers[j]))
            if d < config.get('color_merge_factor', 2.5) * tol:
                mapping[i] = mapping[j]
            else:
                mapping[i] = sid
                sid += 1
    for i, mem in enumerate(members):
        for g in mem:
            g['stream'] = mapping[i]
    return glyphs


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def extract_glyphs(img: np.ndarray, config: Dict,
                   verbose: bool = True) -> List[Dict]:
    ink = _ink_mask(img, config)
    cands = _components(img, config, ink)
    cands = _contrast_filter(img, cands, config)
    cands = _height_groups(cands, config)
    cands = _line_structure_filter(cands, config)
    if verbose:
        print(f'   {len(cands)} glyph candidates after structure filters')

    claimed = np.zeros(ink.shape, bool)

    # layer a: one general OCR pass, used ONLY to find caption words
    # (real letters). Bits are classified geometrically below - far more
    # reliable than OCR for a two-class 0/1 problem.
    words, boxes = _ocr_pass(cands, img.shape[:2])
    glyphs = _boxes_to_glyphs(ink, cands, words, boxes, config,
                              mode='txt', claimed=claimed)

    # layer b: shape classifier (+ keras) on the residual ink -
    # pixel-level, so ink claimed by captions is excluded exactly
    residual = ink.copy()
    residual[claimed] = 0
    res = _contrast_filter(img, _components(img, config, residual),
                           config)
    res = _height_groups(res, config)
    res = _line_structure_filter(res, config)

    leftover = []
    for c in _split_wide(res, config):
        if c.get('kind') == 'bin':         # decided by the run-OCR splitter
            glyphs.append(c)
            continue
        bit, conf = classify_shape(c['mask'], config)
        kbit = _keras_predict(c['mask'], config) if bit is None else None
        if bit is not None:
            c.update(kind='bin', bit=bit, char=bit, conf=conf)
        elif kbit is not None:
            c.update(kind='bin', bit=kbit, char=kbit, conf=0.5)
        else:
            leftover.append(c)
            continue
        glyphs.append(c)

    # rescue pass: an unclassified mark sitting in a line of bits is almost
    # certainly a bit too - guess from gross shape rather than lose it
    for c in leftover:
        in_line = sum(1 for g in glyphs if g['kind'] == 'bin' and
                      abs(g['cy'] - c['cy']) <
                      config.get('neighbor_dy_frac', 0.7) * c['h'] and
                      0.5 < g['h'] / c['h'] < 2.0)
        if in_line < config.get('rescue_min_neighbors', 3):
            continue
        m8 = cv2.copyMakeBorder((c['mask'].astype(np.uint8)) * 255,
                                2, 2, 2, 2, cv2.BORDER_CONSTANT, value=0)
        if _count_holes(m8) >= 1:
            bit = '0'
        else:
            rows = c['mask'][c['h'] // 3: 2 * c['h'] // 3]
            stem = float(rows.sum(axis=1).mean()) if rows.size else c['w']
            bit = '1' if (stem < config.get('rescue_stem_frac', 0.65) *
                          c['w'] or c['w'] / c['h'] <
                          config.get('rescue_one_max_aspect', 0.62)) \
                else '0'
        c.update(kind='bin', bit=bit, char=bit, conf=0.3)
        glyphs.append(c)

    for g in glyphs:
        g['color'] = _ink_color(img, g)

    glyphs = cluster_streams(glyphs, config)
    if verbose:
        nbin = sum(1 for g in glyphs if g['kind'] == 'bin')
        ntxt = len(glyphs) - nbin
        ns = len({g['stream'] for g in glyphs}) if glyphs else 0
        print(f'   {nbin} bits + {ntxt} passthrough chars '
              f'in {ns} colour stream(s)')
    return glyphs
