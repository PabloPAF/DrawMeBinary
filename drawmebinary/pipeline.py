"""
pipeline.py - extract -> decode, one call.
"""
from typing import Dict, Optional

import cv2

from extraction import load_image, extract_glyphs
from decoding import LanguageValidator, decode_glyphs
from security import security_report


def run_pipeline(image_path: str, config: Dict,
                 validator: Optional[LanguageValidator] = None,
                 verbose: bool = True) -> Dict:
    """
    Returns {'text', 'streams', 'all_bits', 'img', 'orig_size', 'security'}.
      text      - full decoded text (streams joined by newline)
      streams   - per colour stream: text, units (char + bbox + colour),
                  quality
      all_bits  - every classified glyph
      img       - the BGR image the glyph coordinates refer to (possibly
                  upscaled; rendering downscales back to orig_size)
      orig_size - (W, H) of the input image
      security  - code-detection report over the decoded text
    """
    img = load_image(image_path)
    H, W = img.shape[:2]
    if verbose:
        print(f'   Image {W}x{H}')
    glyphs = extract_glyphs(img, config, verbose)

    # tiny digits read badly; retry on an upscale and keep the better run
    bins = [g for g in glyphs if g['kind'] == 'bin']
    if config.get('auto_upscale', True) and bins:
        med_h = sorted(g['h'] for g in bins)[len(bins) // 2]
        if med_h < config.get('upscale_below_px', 13):
            factor = config.get('upscale_factor', 2)
            img2 = cv2.resize(img, None, fx=factor, fy=factor,
                              interpolation=cv2.INTER_CUBIC)
            if verbose:
                print(f'   Median glyph {med_h}px -> retrying at '
                      f'{factor}x upscale')
            glyphs2 = extract_glyphs(img2, config, verbose)
            bins2 = [g for g in glyphs2 if g['kind'] == 'bin']
            if len(bins2) >= len(bins):
                img, glyphs = img2, glyphs2

    result = decode_glyphs(glyphs, config, validator, verbose)
    result['img'] = img
    result['orig_size'] = (W, H)
    # decoded content is untrusted: flag code-like text. It is only ever
    # rendered as inert plain text, never evaluated or executed.
    result['security'] = security_report(result['text'])
    if verbose:
        print(f'   Decoded text: {result["text"]!r}')
        if result['security']['code_suspect']:
            found = ', '.join(result['security']['findings'])
            print(f'   SECURITY: decoded content looks like code ({found});'
                  ' rendered as plain text only - it will never be executed')
    return result
