"""
config.py - Configuration for DrawMeBinary.

One flat CONFIG dict plus named PRESETS. Keep it small: every key here is
actually read by the pipeline.
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG = {
    # ------------------------------------------------------------ extraction
    'min_glyph_h': 5,            # px, ignore components shorter than this
    'max_glyph_frac': 0.25,      # ignore components taller than this * image h
    'max_glyph_aspect': 2.0,     # single glyph max width / height
    'max_run_aspect': 8.0,       # merged digit runs up to this are split
    'min_glyph_area': 8,         # px^2
    'min_fill': 0.08,            # ink pixels / bbox area lower bound
    'max_fill': 1.0,             # solid marks are legal: block art
                                 # draws 0/1 as filled squares/bars
    'bg_kernel_frac': 8,         # median-bg kernel = min(H,W) // this
    'auto_upscale': True,        # retry small-glyph images at 2x
    'upscale_below_px': 13,      # ... when median digit is under this
    'upscale_factor': 2,
    'ink_threshold': 40,         # colour distance from local bg = ink
    'height_group_ratio': 1.6,   # split glyph-height clusters at this ratio
    'min_group_size': 2,         # height/line groups smaller than this = noise
    'tesseract_fallback': True,  # use Tesseract for ambiguous glyphs
    'keras_model_path': os.path.join(_ROOT, 'mnist_binary_verifier.keras'),
    'use_keras': True,           # only used if tensorflow is importable

    # --------------------------------------------------- shape classifier
    'shape_hole_min_aspect': 0.32,   # ring with hole + aspect above = '0'
    'shape_one_max_aspect': 0.40,    # narrow solid stroke = '1'
    'shape_hollow_min_aspect': 0.5,  # broken hand-painted ring tests
    'shape_hollow_centre_frac': 0.35,
    'shape_hollow_min_fill': 0.2,
    'shape_bold_one_max_aspect': 0.55,
    'shape_bold_one_min_fill': 0.55,
    'shape_stem_max_aspect': 0.85,   # serif '1': stem narrower than bbox
    'shape_stem_width_frac': 0.62,
    'shape_block_aspect': (0.75, 1.9),  # abstract art: solid block = '0'
    'shape_block_min_fill': 0.85,

    # ------------------------------------------------------------ OCR
    'ocr_target_glyph_px': 40,   # canvas upscaled so glyphs ~ this height
    'ocr_scale_range': (2, 6),
    'ocr_psm': 11,               # sparse text page segmentation
    'ocr_run_psm': 7,            # single line, for merged digit runs
    'ocr_run_target_px': 48,
    'ocr_box_overlap': 0.5,      # char box must overlap its word this much
    'ocr_word_min_conf': 60,     # captions below this confidence ignored
    'ocr_caption_trust_conf': 90,  # above this, unknown words pass as-is
    'binary_word_look_frac': 0.7,   # lookalike fraction for a binary word
    'binary_word_strict_frac': 0.25,

    # -------------------------------------------- segmentation / salvage
    'oversize_area_div': 50,     # comp bigger than W*H/this = oversized
    'salvage_area_factor': 4,    # salvage oversized comps above this * area
    'dominant_color_frac': 0.05,  # histogram share to count as background
    'dominant_color_tol': 70.0,  # distance to background colour = removed
    'min_contrast': 30.0,        # ink vs surround colour distance
    'contrast_ring_px': 3,

    # ----------------------------------------------- merged-digit splitting
    'split_trigger_ratio': 1.2,  # blob wider than this * median width
    'split_ocr_ratio': 1.6,      # ... and this wide: also try OCR on it
    'split_min_piece_frac': 0.7, # smallest piece width tried = this * median
    'split_width_dev_penalty': 0.3,
    'split_cut_cost_penalty': 0.5,
    'split_accept_score': 0.25,
    'split_ocr_bonus': 0.2,

    # ------------------------------------------------- structure filters
    'neighbor_dy_frac': 0.7,     # same-line neighbour: |dy| < this * h
    'neighbor_h_ratio': (0.45, 2.2),
    'neighbor_max_dx_h': 30,     # ... and within this * h horizontally
    'neighbor_min_count': 2,
    'rescue_min_neighbors': 3,   # in-line bits needed to rescue a glyph
    'rescue_stem_frac': 0.65,
    'rescue_one_max_aspect': 0.62,

    # ------------------------------------------------------------ colour
    'color_tolerance': 60,       # Euclidean BGR distance for same stream
    'color_merge_factor': 2.5,   # tiny clusters merge within this * tol
    'min_stream_glyphs': 4,      # smaller streams merge into nearest one

    # ------------------------------------------------------------ decoding
    'line_chain_dx_factor': 4.0,  # neighbour chaining: max dx in glyph sizes
    'line_chain_dy_frac': 0.6,    # ... and max |dy| as fraction of height
    'line_chain_h_ratio': (0.4, 2.5),
    'token_gap_factor': 2.5,     # gap > median_gap * this = token break
    'token_gap_min_frac': 0.25,  # ... and gap > median_height * this
    'space_gap_factor': 1.5,     # unit gap > token width * this = space
    'pair_x_overlap': 0.3,       # min x-overlap to pair nibble tokens
    'nibble_line_frac': 0.6,     # 4-bit tokens needed to call a nibble line
    'byte_line_frac': 0.5,       # 8-bit tokens needed to call a byte line
    'band_merge_overlap': 0.5,   # row y-ranges overlapping this much merge
    'band_break_frac': 2.0,      # band gap > this * height = block break
    'flat_penalty': 0.0,         # handicap of the flat-bitstream candidate;
                                 # 0 = flat wins only when strictly better
                                 # (structured still wins exact ties, as its
                                 # candidates are scored first). Prose that
                                 # wraps mid-byte relies on flat winning.
    'expected_chars_frac': 0.5,  # decoded chars below this share = penalty
    'repair_gate_quality': 0.55,  # per-byte repair only competes below this
                                  # (it can't form multi-byte UTF-8)
    'quality_letter_weight': 0.45,
    'quality_printable_weight': 0.20,
    'quality_word_weight': 0.35,
    'quality_ctrl_penalty': 0.5,
    'encodings': ('utf-8', 'latin-1'),

    # ------------------------------------------------------------ basic mode
    'basic_keep_positions': True,  # draw text where the bits were painted
    'basic_position_height_mult': 1.5,  # font = source glyph height * this
    'basic_margin_frac': 0.10,   # canvas margin (centred layout)
    'basic_min_font_pt': 15,
    'basic_max_font_pt': 110,
    'basic_line_spacing': 1.25,

    # ------------------------------------------------------------ poster mode
    'poster_bbox_multiplier': 0.9,
    'poster_font_variance': 0.6,   # +/- fraction of random size variance
    'poster_min_font_pt': 18,
    'poster_max_font_pt': 130,
    'poster_jitter': 0.6,          # position jitter, fraction of glyph size
    'poster_use_random_fonts': True,
    'poster_width_size_frac': 0.3,  # width contribution to glyph size
    'poster_max_rel_height': 2.2,   # ... capped at this * source height
    'poster_bold_max': 2,           # random extra boldness 0..this
    'poster_seed': 42,              # RNG seed for reproducible posters

    # ------------------------------------------------------ inpainting
    'inpaint_dilate_div': 4,     # dilation iterations = glyph_h // this
    'inpaint_dilate_min': 3,
    'inpaint_radius': 4,

    # ------------------------------------------------------------ security
    'max_input_mb': 50,          # reject input files larger than this
    'max_image_pixels': 64_000_000,  # decompression bomb guard
    'max_image_side': 12_000,    # px, per dimension
    'max_pdf_pages': 50,         # rasterise at most this many pages
    'pdf_scan_bytes': 8_388_608,  # bytes of a PDF scanned for risks

    # ------------------------------------------------ security logging (SIEM)
    'log_service_name': 'drawmebinary',
    'log_service_version': '1.0.0',
    'log_environment': os.environ.get('DMB_ENV', 'development'),
    'log_to_stdout': True,       # one ECS JSON object per line on stdout
    'log_dir': os.path.join(_ROOT, 'logs'),  # rotating JSON file lives here
    'log_to_file': True,
    'log_file_max_mb': 20,       # rotate the JSON log at this size
    'log_file_backups': 5,
    'log_ip_mode': os.environ.get('DMB_LOG_IP_MODE', 'truncate'),
    # ^ 'full' | 'truncate' (zero the host bits) | 'hash' (salted SHA-256)
    # Set DMB_LOG_IP_SALT to a random secret in production (e.g. as an HF
    # Space secret). An empty salt is still GDPR-safe in truncate mode; it
    # only matters when log_ip_mode='hash' — a public salt makes the hash
    # reversible via rainbow table.
    'log_ip_salt': os.environ.get('DMB_LOG_IP_SALT', ''),

    # ------------------------------------------ web app controls (rate limit)
    'rate_limit_enabled': True,
    'rate_limit_max': 30,        # requests per window per client IP
    'rate_limit_window_s': 60,

    # ------------------------------------------------------------ fonts / io
    'font_search_dirs': [
        '/Library/Fonts', '/System/Library/Fonts/Supplemental',
        '/System/Library/Fonts', '/usr/share/fonts',
        'C:/Windows/Fonts',
    ],
    'font_exclude': ('symbol', 'dingbat', 'emoji', 'webdings', 'wingding',
                     'noto color', 'hiragino', 'apple color', 'lastresort'),
    'preferred_font': 'DejaVuSans',   # substring match, basic mode
    'output_dir': os.path.join(_ROOT, 'output'),
}

# Presets scale the levers that actually drive the position-aware render:
# the glyph-height font multiplier and the poster size multiplier (not just
# the max-font cap, which rarely binds now that each character is sized from
# the digits that encoded it).
PRESETS = {
    'sparse': {   # few characters -> large, bold type
        'basic_position_height_mult': 2.6,
        'basic_max_font_pt': 220,
        'poster_bbox_multiplier': 1.4,
        'poster_max_font_pt': 220,
        'poster_bold_max': 3,
    },
    'dense': {    # many characters -> small, tight type
        'basic_position_height_mult': 1.0,
        'basic_max_font_pt': 48,
        'basic_line_spacing': 1.05,
        'poster_bbox_multiplier': 0.65,
        'poster_max_font_pt': 70,
        'poster_font_variance': 0.35,
    },
    'bw': {       # greyscale grids: tune extraction for finer marks
        'ink_threshold': 30,
        'min_glyph_h': 4,
    },
    'story': {    # long narrative text -> compact, calmer poster
        'basic_position_height_mult': 1.15,
        'basic_max_font_pt': 44,
        'basic_line_spacing': 1.1,
        'poster_bbox_multiplier': 0.8,
        'poster_font_variance': 0.3,
        'poster_jitter': 0.3,
    },
}


def get_config_for_preset(preset: str, config: dict = None,
                          presets: dict = None) -> dict:
    """Return a copy of CONFIG with the preset's overrides applied."""
    base = dict(config or CONFIG)
    table = presets or PRESETS
    if preset:
        if preset not in table:
            raise KeyError(f'Unknown preset: {preset!r}')
        base.update(table[preset])
    return base
