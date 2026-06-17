"""
decoding.py - Turn classified glyphs into text.

Layout model (per colour stream):
  * glyphs group into LINE BANDS by vertical overlap,
  * each line splits into TOKENS at horizontal gaps,
  * token lengths decide the format:
      - 8-bit tokens  -> one ASCII/UTF-8 byte each
      - 4-bit tokens  -> nibbles. Two consecutive lines with x-aligned
        tokens pair column-wise (top = high nibble, bottom = low nibble).
        This also covers single-token vertical columns (APOSTATA-style).
        A lone line with an even number of nibbles pairs horizontally.
  * both nibble orders are tried and the whole-stream decode is scored,
  * if the structured decode scores badly, a flat bitstream decode with all
    8 bit offsets is tried as a fallback,
  * passthrough 'txt' glyphs (captions) are kept verbatim, ordered by
    position.

Output 'unit': {'text': str, 'bbox': (x, y, w, h), 'color': (b,g,r),
                'kind': 'bin'|'txt'}
"""
import unicodedata
from typing import Dict, List, Optional, Tuple

from config import CONFIG

# ---------------------------------------------------------------- language
_COMMON_WORDS = set('''
the a an and or of to in is it was were be been i you he she we they this
that not no yes all here there what who how why when where my your his her
its our their as at by for from on with so but if then than too very can
will just here was here love hate life time day man men woman women world
stupid human ruined it pass shall not you enough
el la los las un una unos unas y o de del a en es son era eran fue ser
estar no si que quien como cuando donde por para con sin su mi tu nuestro
añoro amor vida tiempo dia hombre mujer mundo aqui alli pasado basta
'''.split())

try:
    from spellchecker import SpellChecker
    _SPELL = {
        'en': SpellChecker(language='en', distance=0),
        'es': SpellChecker(language='es', distance=0),
    }
    _SPELL_FUZZY = {
        'en': SpellChecker(language='en', distance=1),
        'es': SpellChecker(language='es', distance=1),
    }
except Exception:                                     # pragma: no cover
    _SPELL = None
    _SPELL_FUZZY = None


class LanguageValidator:
    """Scores decoded text; EN + ES."""

    def __init__(self, config: Optional[Dict] = None):
        self.cfg = config or CONFIG

    def word_score(self, text: str) -> float:
        words = [w.strip(".,!?:;\"'()").lower()
                 for w in text.split()]
        words = [w for w in words if w.isalpha()]
        if not words:
            return 0.0
        # length-weighted: 'shall' counts more than the 'sh'+'all' a wrong
        # line join would produce, so complete words win the decode vote
        total = sum(len(w) for w in words)
        hits = 0
        for w in words:
            if len(w) < 2:
                continue
            wn = unicodedata.normalize('NFC', w)
            if wn in _COMMON_WORDS or (
                    _SPELL is not None and
                    (wn in _SPELL['en'] or wn in _SPELL['es'])):
                hits += len(w)
        return hits / max(1, total)

    def word_plausible(self, word: str) -> bool:
        """The word is, or is one OCR slip away from, a real EN/ES word.
        Very short words must be common ones: the frequency wordlists
        contain junk two-letter tokens ('hl') that would otherwise let
        OCR noise pass as captions."""
        w = unicodedata.normalize('NFC', word.lower())
        if not w:
            return False
        if w in _COMMON_WORDS:
            return True
        if len(w) <= 2:
            return False
        if _SPELL is not None and (w in _SPELL['en'] or w in _SPELL['es']):
            return True
        if _SPELL_FUZZY is not None and len(w) >= 4:
            for lang in ('en', 'es'):
                c = _SPELL_FUZZY[lang].correction(w)
                if c is not None and c != w:
                    return True
        return False

    def correct_word(self, word: str) -> str:
        """Fix a one-slip OCR misread ('rutned' -> 'ruined'); otherwise
        return the word unchanged."""
        w = word.lower()
        if not w.isalpha() or len(w) < 4 or self.word_score(w) > 0:
            return word
        if _SPELL_FUZZY is not None:
            for lang in ('en', 'es'):
                c = _SPELL_FUZZY[lang].correction(w)
                if c is not None and c != w:
                    return c.upper() if word.isupper() else \
                        c.capitalize() if word[0].isupper() else c
        return word

    def quality(self, text: str) -> float:
        """0..1 overall plausibility of a decoded string."""
        if not text:
            return 0.0
        printable = sum(1 for c in text
                        if c.isprintable() or c in '\n\t')
        letters = sum(1 for c in text
                      if c.isalpha() or c in ' \n.,!?\'"-:;')
        n = len(text)
        ctrl = n - printable
        score = (self.cfg['quality_letter_weight'] * (letters / n) +
                 self.cfg['quality_printable_weight'] * (printable / n) +
                 self.cfg['quality_word_weight'] * self.word_score(text))
        score -= self.cfg['quality_ctrl_penalty'] * (ctrl / n)
        return max(0.0, min(1.0, score))


# ---------------------------------------------------------------- grouping
def group_lines(glyphs: List[Dict], config: Dict) -> List[List[Dict]]:
    """
    Cluster glyphs into text lines by neighbour chaining: two glyphs join
    the same line when they are horizontal neighbours (small dy, moderate
    dx). Chaining follows slightly tilted lines, where a global horizontal
    band would split or interleave them.
    """
    if not glyphs:
        return []
    # a tall '1' bar (block art) hangs below its row: anchor such
    # glyphs by their top edge instead of their centre
    med_h = sorted(g['h'] for g in glyphs)[len(glyphs) // 2]
    eff = [g['y'] + 0.5 * min(g['h'], med_h) for g in glyphs]
    idx = sorted(range(len(glyphs)), key=lambda i: glyphs[i]['cx'])
    parent = list(range(len(glyphs)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    dxf = config.get('line_chain_dx_factor', 4.0)
    dyf = config.get('line_chain_dy_frac', 0.6)
    rlo, rhi = config.get('line_chain_h_ratio', (0.4, 2.5))
    for pos, i in enumerate(idx):
        gi = glyphs[i]
        for j in idx[pos + 1:]:
            gj = glyphs[j]
            dx = gj['cx'] - gi['cx']
            if dx > dxf * max(gi['w'], gi['h']):
                break
            dy = abs(eff[j] - eff[i])
            if dy < dyf * min(med_h, gi['h'], gj['h']) and \
                    rlo < gj['h'] / gi['h'] < rhi:
                union(i, j)

    lines: Dict[int, List[Dict]] = {}
    for i, g in enumerate(glyphs):
        lines.setdefault(find(i), []).append(g)
    out = sorted(lines.values(),
                 key=lambda l: sum(g['cy'] for g in l) / len(l))
    return [sorted(l, key=lambda g: g['cx']) for l in out]


def _bbox_union(glyphs: List[Dict]) -> Tuple[int, int, int, int]:
    x0 = min(g['x'] for g in glyphs)
    y0 = min(g['y'] for g in glyphs)
    x1 = max(g['x'] + g['w'] for g in glyphs)
    y1 = max(g['y'] + g['h'] for g in glyphs)
    return (x0, y0, x1 - x0, y1 - y0)


def tokens_in_line(line: List[Dict], config: Dict) -> List[Dict]:
    """Split a sorted line into tokens at horizontal gaps."""
    if not line:
        return []
    med_h = sorted(g['h'] for g in line)[len(line) // 2]
    gaps = []
    for a, b in zip(line, line[1:]):
        gaps.append(b['x'] - (a['x'] + a['w']))
    med_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0
    thresh = max(med_gap * config.get('token_gap_factor', 2.5),
                 med_h * config.get('token_gap_min_frac', 0.45))
    toks, cur = [], [line[0]]
    for (a, b), gap in zip(zip(line, line[1:]), gaps):
        if gap > thresh:
            toks.append(cur)
            cur = [b]
        else:
            cur.append(b)
    toks.append(cur)
    out = []
    for t in toks:
        kinds = {g['kind'] for g in t}
        kind = 'bin' if kinds == {'bin'} else 'txt'
        out.append({'kind': kind,
                    'bits': ''.join(g['bit'] or '' for g in t),
                    'text': ''.join(g['char'] or '' for g in t),
                    'bbox': _bbox_union(t),
                    'glyphs': t})
    return out


# ---------------------------------------------------------------- decoding
def _byte_to_char(bits: str, config: Dict) -> str:
    b = bytes([int(bits, 2)])
    for enc in config.get('encodings', ('utf-8', 'latin-1')):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return '�'


def _bits_to_text(bits: str, config: Dict) -> str:
    raw = bytes(int(bits[i:i + 8], 2)
                for i in range(0, len(bits) - len(bits) % 8, 8))
    for enc in config.get('encodings', ('utf-8', 'latin-1')):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('latin-1', errors='replace')


def _utf8_len(b: int) -> int:
    """Number of bytes in the UTF-8 sequence that starts with byte b."""
    if b < 0x80:
        return 1
    if b >> 5 == 0b110:
        return 2
    if b >> 4 == 0b1110:
        return 3
    if b >> 3 == 0b11110:
        return 4
    return 1                                   # stray continuation byte


def _make_cells(bits: str, glyphs: List[Dict]) -> List[Dict]:
    """Split a bit string into one cell per byte, each carrying the 8 glyphs
    that encoded it (when the glyph count matches the bit count)."""
    gl = sorted(glyphs, key=lambda g: g['cx'])
    aligned = len(gl) == len(bits)
    cells = []
    for k in range(len(bits) // 8):
        chunk = bits[k * 8:(k + 1) * 8]
        sub = gl[k * 8:(k + 1) * 8] if aligned else []
        cells.append({'byte': int(chunk, 2), 'glyphs': sub})
    return cells


def _cells_to_units(cells: List[Dict], config: Dict,
                    fallback_bbox=(0, 0, 1, 1)) -> List[Dict]:
    """
    Decode an ordered list of byte cells into character units, honouring
    multi-byte UTF-8: a character that spans several bytes is built from all
    of their glyphs, so accented letters (ñ, í, ó), em-dashes and smart
    quotes survive instead of splitting into mojibake (Ã±, â\\x80\\x94 ...).
    Falls back to latin-1 for a byte that is not valid UTF-8.
    """
    prefer_utf8 = 'utf-8' in config.get('encodings', ('utf-8', 'latin-1'))
    units: List[Dict] = []
    i, n = 0, len(cells)
    while i < n:
        b0 = cells[i]['byte']
        ln = _utf8_len(b0) if prefer_utf8 else 1
        ln = min(ln, n - i)
        raw = bytes(cells[i + k]['byte'] for k in range(ln))
        ch = None
        if prefer_utf8 and ln > 1:
            try:
                dec = raw.decode('utf-8')
                ch = dec if len(dec) == 1 else None
            except UnicodeDecodeError:
                ch = None
        if ch is None:                          # single byte, utf-8 then latin-1
            ln = 1
            one = bytes([b0])
            ch = (one.decode('utf-8') if prefer_utf8 and b0 < 0x80
                  else one.decode('latin-1'))
        group = sum((cells[i + k]['glyphs'] for k in range(ln)), [])
        units.append({'text': ch, 'kind': 'bin', 'glyphs': group,
                      'bbox': _bbox_union(group) if group else fallback_bbox})
        i += ln
    return units


def _spread_text_units(text: str, glyphs: List[Dict],
                       fallback_bbox=(0, 0, 1, 1)) -> List[Dict]:
    """Spread already-decoded text proportionally across glyphs (used when
    the byte boundaries are unknown, e.g. after error repair)."""
    gl = sorted(glyphs, key=lambda g: g['cx'])
    m, nch = len(gl), max(1, len(text))
    units = []
    for k, ch in enumerate(text):
        sub = gl[(k * m) // nch:((k + 1) * m) // nch]
        units.append({'text': ch, 'kind': 'bin', 'glyphs': sub or gl,
                      'bbox': _bbox_union(sub) if sub else fallback_bbox})
    return units


def _x_overlap(b1, b2) -> float:
    x0 = max(b1[0], b2[0])
    x1 = min(b1[0] + b1[2], b2[0] + b2[2])
    return max(0.0, x1 - x0) / max(1.0, min(b1[2], b2[2]))


def _pair_tokens(top: List[Dict], bot: List[Dict],
                 config: Dict) -> Optional[List[Tuple[Dict, Dict]]]:
    """Match nibble tokens of two lines by x-overlap. None if no match."""
    min_ov = config.get('pair_x_overlap', 0.3)
    pairs, used = [], set()
    for t in top:
        best, bov = None, min_ov
        for j, b in enumerate(bot):
            if j in used:
                continue
            ov = _x_overlap(t['bbox'], b['bbox'])
            if ov > bov:
                best, bov = j, ov
        if best is None:
            return None
        used.add(best)
        pairs.append((t, bot[best]))
    if len(used) != len(bot):
        return None
    return pairs


def _merge_bbox(b1, b2):
    x0 = min(b1[0], b2[0]); y0 = min(b1[1], b2[1])
    x1 = max(b1[0] + b1[2], b2[0] + b2[2])
    y1 = max(b1[1] + b1[3], b2[1] + b2[3])
    return (x0, y0, x1 - x0, y1 - y0)


def _nibble_tokens(toks: List[Dict],
                   config: Optional[Dict] = None) -> Optional[List[Dict]]:
    """The line's 4-bit tokens, if the line is nibble-dominated."""
    cfg = config or CONFIG
    bt = [t for t in toks if t['kind'] == 'bin']
    if not bt:
        return None
    # a lone 4-bit group falsely split into pieces is still one nibble
    if sum(len(t['bits']) for t in bt) == 4 and len(bt) > 1:
        merged = {'kind': 'bin',
                  'bits': ''.join(t['bits'] for t in bt),
                  'text': ''.join(t['text'] for t in bt),
                  'bbox': _merge_bbox(bt[0]['bbox'], bt[-1]['bbox']),
                  'glyphs': sum((t['glyphs'] for t in bt), [])}
        return [merged]
    n4 = [t for t in bt if len(t['bits']) == 4]
    if len(n4) >= cfg['nibble_line_frac'] * len(bt) and \
            all(len(t['bits']) <= 6 for t in bt):
        return n4
    return None


def _byte_tokens(toks: List[Dict],
                 config: Optional[Dict] = None) -> Optional[List[Dict]]:
    """The line's 8-bit-multiple tokens, if the line is byte-dominated.
    Damaged tokens (a glyph lost to artwork) are skipped, not fatal."""
    cfg = config or CONFIG
    bt = [t for t in toks if t['kind'] == 'bin']
    n8 = [t for t in bt if len(t['bits']) % 8 == 0 and len(t['bits']) >= 8]
    if bt and len(n8) >= max(1, cfg['byte_line_frac'] * len(bt)):
        return n8
    return None


def _decode_structured(token_lines: List[List[Dict]], config: Dict,
                       top_high: bool = True) -> List[Dict]:
    """token_lines -> list of units. top_high selects nibble order."""
    units: List[Dict] = []
    i = 0
    while i < len(token_lines):
        toks = token_lines[i]
        bins = [t for t in toks if t['kind'] == 'bin']
        txts = [t for t in toks if t['kind'] == 'txt']
        for t in txts:
            units.append({'text': t['text'], 'bbox': t['bbox'],
                          'kind': 'txt', 'glyphs': t['glyphs']})
        if not bins:
            i += 1
            continue
        nibs = _nibble_tokens(toks, config)
        bytes_ = _byte_tokens(toks, config) if nibs is None else None
        if bytes_:
            all_bins = [t for t in toks if t['kind'] == 'bin']
            # one continuous byte stream across the row's byte tokens (a
            # multi-byte UTF-8 char can span adjacent tokens), decoded
            # UTF-8-aware so accents survive
            btoks = sorted(bytes_, key=lambda t: t['bbox'][0])
            cells = []
            for t in btoks:
                cells += _make_cells(t['bits'], t['glyphs'])
            clean_units = _cells_to_units(cells, config, all_bins[0]['bbox'])
            if len(bytes_) < len(all_bins):
                # damaged tokens present: maybe the gaps are spurious and
                # the row is one continuous (slightly corrupted) stream -
                # decode both ways and keep whichever reads better
                bits = ''.join(t['bits'] for t in
                               sorted(all_bins, key=lambda t: t['bbox'][0]))
                whole = _bits_to_text(bits, config) \
                    if len(bits) % 8 == 0 else _repair_decode(bits, config)
                clean_txt = ''.join(u['text'] for u in clean_units)
                sc = lambda txt: sum(_char_score(c) for c in txt)
                if whole and sc(whole) > sc(clean_txt):
                    gl = sorted(sum((t['glyphs'] for t in all_bins), []),
                                key=lambda g: g['cx'])
                    units += _spread_text_units(whole, gl,
                                                all_bins[0]['bbox'])
                    i += 1
                    continue
            units += clean_units
            i += 1
            continue
        if nibs:
            bins = nibs
            nxt = None
            if i + 1 < len(token_lines):
                nxt = _nibble_tokens(token_lines[i + 1], config)
            pairs = _pair_tokens(bins, nxt, config) if nxt else None
            if pairs:
                # each pair = one byte; build a cell stream so multi-byte
                # UTF-8 characters spanning consecutive pairs decode right
                cells = [{'byte': int((t if top_high else b)['bits'] +
                                      (b if top_high else t)['bits'], 2),
                          'glyphs': t['glyphs'] + b['glyphs']}
                         for t, b in pairs]
                units += _cells_to_units(cells, config,
                                         _merge_bbox(pairs[0][0]['bbox'],
                                                     pairs[0][1]['bbox']))
                for t in token_lines[i + 1]:
                    if t['kind'] == 'txt':
                        units.append({'text': t['text'], 'bbox': t['bbox'],
                                      'kind': 'txt', 'glyphs': t['glyphs']})
                i += 2
                continue
            if len(bins) % 2 == 0:          # pair horizontally in-line
                cells = [{'byte': int((a if top_high else b)['bits'] +
                                      (b if top_high else a)['bits'], 2),
                          'glyphs': a['glyphs'] + b['glyphs']}
                         for a, b in zip(bins[0::2], bins[1::2])]
                units += _cells_to_units(cells, config, bins[0]['bbox'])
                i += 1
                continue
        # irregular line: concatenate bits, decode as bytes if it divides
        bits = ''.join(t['bits'] for t in bins)
        if len(bits) >= 8:
            gl = sorted(sum((t['glyphs'] for t in bins), []),
                        key=lambda g: g['cx'])
            if len(bits) % 8 == 0:
                # clean: byte-aligned, so build UTF-8-aware cells
                units += _cells_to_units(_make_cells(bits, gl), config,
                                         bins[0]['bbox'])
            else:
                # a digit was lost/gained: repair, then spread proportionally
                units += _spread_text_units(_repair_decode(bits, config),
                                            gl, bins[0]['bbox'])
        i += 1
    return units


def _assign_rows(units: List[Dict],
                 row_of_glyph: Dict[int, object]) -> None:
    """Tag each unit with the row index of its source glyphs (for stable
    reading order even when lines are tilted)."""
    for u in units:
        rows = [row_of_glyph[id(g)] for g in u.get('glyphs', [])
                if id(g) in row_of_glyph]
        u['row'] = min(rows, key=str) if rows else None


def units_to_lines(units: List[Dict], config: Dict) -> List[Dict]:
    """
    Group decoded units into visual lines, top-down.
    Returns [{'text', 'bbox', 'y0', 'y1'}]; within a line units are ordered
    by x and big horizontal gaps become spaces. Used both for assembling
    the final text and for rendering text at its original position.
    """
    if not units:
        return []
    # group by source row (tilt-proof), then merge rows whose y-ranges
    # overlap (captions sitting beside nibble pairs interleave by x)
    rows: Dict[object, List[Dict]] = {}
    for k, u in enumerate(units):
        key = u.get('row')
        rows.setdefault(('?', k) if key is None else key, []).append(u)
    bands: List[Dict] = []
    for key in rows:
        us = rows[key]
        y0 = min(u['bbox'][1] for u in us)
        y1 = max(u['bbox'][1] + u['bbox'][3] for u in us)
        bands.append({'y0': y0, 'y1': y1, 'units': us})
    bands.sort(key=lambda b: b['y0'])
    merged: List[Dict] = []
    for b in bands:
        if merged:
            m = merged[-1]
            ov = min(m['y1'], b['y1']) - max(m['y0'], b['y0'])
            if ov > config.get('band_merge_overlap', 0.5) * \
                    min(m['y1'] - m['y0'], b['y1'] - b['y0']):
                m['units'].extend(b['units'])
                m['y0'] = min(m['y0'], b['y0'])
                m['y1'] = max(m['y1'], b['y1'])
                continue
        merged.append(b)

    lines: List[Dict] = []
    for b in merged:
        bu = sorted(b['units'], key=lambda u: u['bbox'][0])
        line = ''
        for i, u in enumerate(bu):
            if i > 0:
                prev = bu[i - 1]['bbox']
                gap = u['bbox'][0] - (prev[0] + prev[2])
                ref = max(8.0, min(prev[2], u['bbox'][2]) /
                          max(1, len(bu[i - 1]['text'])))
                if (gap > ref * config.get('space_gap_factor', 1.5)
                        and not line.endswith(' ')):
                    line += ' '
            line += u['text']
        x0 = min(u['bbox'][0] for u in bu)
        x1 = max(u['bbox'][0] + u['bbox'][2] for u in bu)
        lines.append({'text': line, 'y0': b['y0'], 'y1': b['y1'],
                      'units': bu,
                      'bbox': (x0, b['y0'], x1 - x0, b['y1'] - b['y0'])})
    return lines


def _units_to_text(units: List[Dict], config: Dict) -> str:
    """
    Assemble the stream text from its visual lines. Between lines, the
    joiner depends on the vertical gap: tight leading = words may wrap
    mid-word (join with ''), a clearly larger gap = separate blocks.
    """
    lines = units_to_lines(units, config)
    if not lines:
        return ''
    bands = lines
    parts = [l['text'] for l in lines]

    if len(bands) <= 1:
        text = parts[0]
    else:
        gaps = [max(0.0, bands[i + 1]['y0'] - bands[i]['y1'])
                for i in range(len(bands) - 1)]
        med_h = sorted(b['y1'] - b['y0'] for b in bands)[len(bands) // 2]
        join = config.get('_band_join', 'rule')
        text = parts[0]
        for gap, part in zip(gaps, parts[1:]):
            if join == 'rule':
                if gap > config.get('band_break_frac', 2.0) * med_h:
                    text += '\n'    # clearly separate blocks
                # else tight leading: words may wrap mid-word
            else:
                text += join        # forced by the decode-quality vote
            text += part
    while '  ' in text:
        text = text.replace('  ', ' ')
    return '\n'.join(ln.strip() for ln in text.split('\n')).strip()


def _char_score(ch: str) -> float:
    if ch.isalpha() or ch == ' ':
        return 1.0
    if ch.isdigit() or ch in '.,!?\'"-:;()':
        return 0.5
    if ch.isprintable():
        return 0.1
    return -1.5


def _repair_decode(bits: str, config: Dict) -> str:
    """
    Error-tolerant bitstream decode: a misread that inserts or deletes a
    single digit shifts every byte after it. A small DP walks the stream
    in 8-bit steps but may occasionally take a 9-bit step (skip a bogus
    digit) or a 7-bit step (a digit was lost), paying a penalty, so the
    text after the error is recovered instead of turning to noise.
    """
    n = len(bits)
    NEG = float('-inf')
    best = [NEG] * (n + 1)
    back: List[Optional[Tuple[int, str]]] = [None] * (n + 1)
    best[0] = 0.0
    skip_pen = config.get('repair_skip_penalty', 1.2)
    lost_pen = config.get('repair_lost_penalty', 1.6)
    flip_pen = config.get('repair_flip_penalty', 0.7)
    for i in range(n + 1):
        if best[i] == NEG:
            continue
        if i + 8 <= n:                       # normal byte
            chunk = bits[i:i + 8]
            ch = _byte_to_char(chunk, config)
            v = best[i] + _char_score(ch)
            if v > best[i + 8]:
                best[i + 8], back[i + 8] = v, (i, ch)
            # a single misread digit flips one bit of the byte: try the
            # 8 one-flip variants when the literal byte reads badly
            if _char_score(ch) < 0.5:
                for k in range(8):
                    fb = chunk[:k] + ('1' if chunk[k] == '0' else '0') + \
                        chunk[k + 1:]
                    fch = _byte_to_char(fb, config)
                    fv = best[i] + _char_score(fch) - flip_pen
                    if fv > best[i + 8]:
                        best[i + 8], back[i + 8] = fv, (i, fch)
        if i + 9 <= n:                       # one inserted digit: skip it
            ch = _byte_to_char(bits[i + 1:i + 9], config)
            v = best[i] + _char_score(ch) - skip_pen
            if v > best[i + 9]:
                best[i + 9], back[i + 9] = v, (i, ch)
        if i + 7 <= n:                       # one lost digit: char unknown
            v = best[i] - lost_pen
            if v > best[i + 7]:
                best[i + 7], back[i + 7] = v, (i, '?')
    end = max(range(max(0, n - 7), n + 1), key=lambda j: best[j])
    if best[end] == NEG:
        return ''
    chars: List[str] = []
    j = end
    while j > 0 and back[j] is not None:
        j, ch = back[j]
        chars.append(ch)
    return ''.join(reversed(chars))


def _flat_fallback(glyphs: List[Dict], config: Dict,
                   validator: LanguageValidator) -> Tuple[str, List[Dict]]:
    """Reading-order bitstream: best of 8 offsets and the error-repaired
    decode, whichever scores higher. Each decoded character becomes a unit
    positioned over the ~8 glyphs that encoded it, so renders stay at the
    original bit positions even for this fallback."""
    bins = [g for g in glyphs if g['kind'] == 'bin']
    lines = group_lines(bins, config)
    seq = [g for line in lines for g in line]
    row_of = {id(g): r for r, line in enumerate(lines) for g in line}
    bits = ''.join(g['bit'] for g in seq)
    best, best_q = '', -1.0
    for off in range(8):
        txt = _bits_to_text(bits[off:], config)
        q = validator.quality(txt)
        if q > best_q:
            best, best_q = txt, q
    # The repair decoder works one byte at a time (latin-1), so it cannot
    # produce multi-byte UTF-8 and would turn 'ñ' into 'Ã±'. Only let it
    # compete when the stream actually looks damaged - a misaligned bit
    # length, or a poor straight decode - never on a clean, well-decoding
    # stream where it would only corrupt accents.
    damaged = (len(bits) % 8 != 0 or
               best_q < config.get('repair_gate_quality', 0.55))
    if damaged:
        rtxt = _repair_decode(bits, config)
        if rtxt and validator.quality(rtxt) > best_q:
            best = rtxt
    if not seq or not best:
        return best, [{'text': best, 'bbox': (0, 0, 1, 1),
                       'kind': 'bin', 'glyphs': bins}]
    units = []
    m, n = len(seq), len(best)
    for k, ch in enumerate(best):
        sub = seq[(k * m) // n:max((k * m) // n + 1, ((k + 1) * m) // n)]
        # anchor the character to the row where its bits start: a char
        # whose byte wraps across rows must not get a bbox spanning both
        row = row_of[id(sub[0])]
        anchor = [g for g in sub if row_of[id(g)] == row] or sub
        units.append({'text': ch, 'bbox': _bbox_union(anchor),
                      'kind': 'bin', 'glyphs': sub,
                      'row': row})
    return best, units


# ---------------------------------------------------------------- public
def decode_stream(glyphs: List[Dict], config: Dict,
                  validator: LanguageValidator,
                  verbose: bool = True) -> Dict:
    """Decode one colour stream -> {'text', 'units', 'quality', 'color'}."""
    # Binary glyphs and caption letters band separately: a tall caption can
    # otherwise bridge two nibble rows into one line and break the pairing.
    bins = [g for g in glyphs if g['kind'] == 'bin']
    txts = [g for g in glyphs if g['kind'] == 'txt']

    token_lines = [tokens_in_line(l, config)
                   for l in group_lines(bins, config)]
    row_of_glyph: Dict[int, object] = {}
    for r, line in enumerate(token_lines):
        for t in line:
            for g in t['glyphs']:
                row_of_glyph[id(g)] = r

    txt_units = []
    for j, l in enumerate(group_lines(txts, config)):
        for t in tokens_in_line(l, config):
            txt_units.append({'text': validator.correct_word(t['text']),
                              'bbox': t['bbox'],
                              'kind': 'txt', 'glyphs': t['glyphs'],
                              'row': ('t', j)})

    n_txt_chars = sum(len(u['text']) for u in txt_units)

    def scored(text: str) -> float:
        """Quality with a penalty when most of the stream's content (bits
        AND caption characters) produced no text."""
        q = validator.quality(text)
        expected = max(1.0, config.get('expected_chars_frac', 0.5) *
                       (sum(len(g['bit'] or '') for g in bins) / 8 +
                        n_txt_chars))
        if len(text) < expected:
            q *= len(text) / expected
        return q

    # vote over nibble order x line-join strategy; ties favour the first
    candidates = []
    for top_high in (True, False):
        units = _decode_structured(token_lines, config, top_high)
        _assign_rows(units, row_of_glyph)
        units += txt_units
        for join in ('rule', '', ' ', '\n'):
            cfg = dict(config, _band_join=join)
            text = _units_to_text(units, cfg)
            candidates.append((scored(text), text, units, join))
    # the flat bitstream reading always competes: it wins when lines wrap
    # mid-byte and the token structure misleads
    ftext, funits = _flat_fallback(glyphs, config, validator)
    candidates.append((scored(ftext) -
                       config.get('flat_penalty', 0.05), ftext, funits,
                       'rule'))
    q, text, units, join = max(candidates, key=lambda c: c[0])

    color = glyphs[0].get('color', (0, 0, 0)) if glyphs else (0, 0, 0)
    return {'text': text, 'units': units, 'quality': q, 'color': color,
            'band_join': join}


def _unit_colors(d: Dict) -> None:
    """Tag every unit with the median ink colour of its own glyphs, so a
    unified (colour-agnostic) decode still renders each character in the
    colour it was painted with."""
    import statistics
    for u in d.get('units', []):
        cols = [g.get('color') for g in u.get('glyphs', [])
                if g.get('color') is not None]
        if cols:
            u['color'] = tuple(int(statistics.median(c[i] for c in cols))
                               for i in range(3))


def decode_glyphs(glyphs: List[Dict], config: Dict,
                  validator: Optional[LanguageValidator] = None,
                  verbose: bool = True) -> Dict:
    """
    Decode all streams. Two readings compete: one stream per ink colour
    (each colour is an independent message) versus a single unified stream
    (colour is decorative; a row mixes colours). The language score over
    all glyphs picks the winner.
    """
    validator = validator or LanguageValidator()
    streams = {}
    for g in glyphs:
        streams.setdefault(g.get('stream', 0), []).append(g)
    decoded = []
    for sid, sg in sorted(streams.items(),
                          key=lambda kv: -len(kv[1])):
        d = decode_stream(sg, config, validator, verbose)
        d['stream'] = sid
        d['n_glyphs'] = len(sg)
        decoded.append(d)
        if verbose:
            print(f"   stream {sid}: {len(sg)} glyphs, "
                  f"quality {d['quality']:.2f} -> {d['text']!r}")

    total = sum(d['n_glyphs'] for d in decoded)
    per_color_q = sum(d['quality'] * d['n_glyphs']
                      for d in decoded) / max(1, total)
    # the unified reading only competes when colour separation looks
    # doubtful: many tiny streams (braided art) or weak per-colour decodes
    if len(decoded) > 1 and (
            len(decoded) >= config.get('unified_min_streams', 4) or
            per_color_q < config.get('unified_weak_q', 0.5)):
        uni = decode_stream(glyphs, config, validator, verbose)
        uni['stream'] = 'unified'
        uni['n_glyphs'] = total
        if uni['quality'] > per_color_q + \
                config.get('unified_margin', 0.05):
            if verbose:
                print(f"   unified decode wins: {uni['quality']:.2f} vs "
                      f"{per_color_q:.2f} -> {uni['text']!r}")
            _unit_colors(uni)
            decoded = [uni]

    text = '\n'.join(d['text'] for d in decoded if d['text'])
    return {'text': text, 'streams': decoded, 'all_bits': glyphs}
