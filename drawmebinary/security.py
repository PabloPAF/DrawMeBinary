"""
security.py - Defensive guards around the DrawMeBinary pipeline.

The decoded message hidden in an artwork is UNTRUSTED DATA. So is the file
that carries it. This module hardens every boundary where that data enters
or leaves the program, without touching the pipeline itself:

  * validate_input_file  - the file really is an image/PDF (magic bytes,
    not just the extension), is not an executable in disguise, is not a
    decompression bomb, and respects size limits.
  * sanitize_text        - decoded text is made terminal-safe (ANSI escape
    sequences and control characters become visible escapes) before it is
    printed or written to disk.
  * detect_code          - heuristics that flag decoded content which looks
    like script/shell/SQL/HTML code. Findings are reported; the content is
    still rendered - as inert plain text - but NEVER evaluated or executed.
  * safe_basename / safe_output_path - output filenames derived from input
    names cannot escape the output directory (path traversal, separators,
    control characters).
  * scan_pdf_risks       - active-content markers inside PDFs (JavaScript,
    Launch actions, embedded files) are reported. Pages are only ever
    rasterised to pixels; no PDF action is ever executed.

Guarantee: nothing in this project passes decoded content to eval, exec,
os.system, a subprocess, or any interpreter. tests.py enforces this with a
source-level audit (test_no_dynamic_execution_in_source).
"""
import os
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from config import CONFIG


class SecurityError(Exception):
    """Input rejected by a security check. The message is user-safe."""


# --------------------------------------------------------------------------
# 1. input file validation
# --------------------------------------------------------------------------
# magic-byte signatures of the formats we accept
_IMAGE_SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'\xff\xd8\xff', 'jpeg'),
    (b'BM', 'bmp'),
    (b'II*\x00', 'tiff'),
    (b'MM\x00*', 'tiff'),
)
_PDF_SIGNATURE = b'%PDF-'

# signatures of things that must never be processed, whatever the extension
_EXECUTABLE_SIGNATURES = (
    (b'MZ', 'Windows executable (PE)'),
    (b'\x7fELF', 'Linux executable (ELF)'),
    (b'\xfe\xed\xfa\xce', 'macOS executable (Mach-O)'),
    (b'\xfe\xed\xfa\xcf', 'macOS executable (Mach-O)'),
    (b'\xcf\xfa\xed\xfe', 'macOS executable (Mach-O)'),
    (b'\xca\xfe\xba\xbe', 'macOS universal binary / Java class'),
    (b'#!', 'script with shebang'),
    (b'PK\x03\x04', 'zip archive'),
    (b'\x1f\x8b', 'gzip archive'),
    (b'7z\xbc\xaf', '7-zip archive'),
    (b'Rar!', 'rar archive'),
)

_EXT_FAMILY = {
    '.png': 'png', '.jpg': 'jpeg', '.jpeg': 'jpeg', '.bmp': 'bmp',
    '.tif': 'tiff', '.tiff': 'tiff', '.pdf': 'pdf',
}


def validate_input_file(path: str, config: Optional[Dict] = None) -> Dict:
    """
    Validate an input file before anything opens it.
    Returns {'kind': 'image'|'pdf', 'warnings': [...]} or raises
    SecurityError with a user-safe message.
    """
    cfg = config or CONFIG
    warnings: List[str] = []

    if not os.path.exists(path):
        raise SecurityError(f'file not found: {path}')
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise SecurityError('input is not a regular file')

    size = os.path.getsize(real)
    max_mb = cfg.get('max_input_mb', 50)
    if size == 0:
        raise SecurityError('input file is empty')
    if size > max_mb * 1024 * 1024:
        raise SecurityError(
            f'input file is {size / 1e6:.0f} MB; the limit is {max_mb} MB')

    ext = os.path.splitext(path)[1].lower()
    if ext not in _EXT_FAMILY:
        raise SecurityError(
            f'extension {ext!r} is not allowed '
            f'(accepted: {", ".join(sorted(_EXT_FAMILY))})')

    with open(real, 'rb') as f:
        head = f.read(16)

    for sig, name in _EXECUTABLE_SIGNATURES:
        if head.startswith(sig):
            raise SecurityError(
                f'file content is a {name}, not an image - refused')

    if head.startswith(_PDF_SIGNATURE):
        family = 'pdf'
    else:
        family = next((fam for sig, fam in _IMAGE_SIGNATURES
                       if head.startswith(sig)), None)
    if family is None:
        raise SecurityError(
            'file content does not match any supported image or PDF '
            'format (extension spoofing?)')
    if family != _EXT_FAMILY[ext]:
        warnings.append(
            f'extension says {_EXT_FAMILY[ext]} but content is {family}; '
            f'treating it as {family}')

    if family == 'pdf':
        warnings += scan_pdf_risks(real, cfg)
        return {'kind': 'pdf', 'warnings': warnings}

    _check_image_dimensions(real, cfg)
    return {'kind': 'image', 'warnings': warnings}


def _check_image_dimensions(path: str, cfg: Dict) -> None:
    """Decompression-bomb guard: header-declared dimensions must be sane
    BEFORE any full decode happens."""
    from PIL import Image
    max_px = cfg.get('max_image_pixels', 64_000_000)
    max_side = cfg.get('max_image_side', 12_000)
    Image.MAX_IMAGE_PIXELS = max_px        # PIL's own bomb guard
    try:
        with Image.open(path) as im:
            w, h = im.size
            im.verify()                    # header consistency, no decode
    except SecurityError:
        raise
    except Exception as exc:
        raise SecurityError(f'image failed validation: {exc}') from None
    if w > max_side or h > max_side:
        raise SecurityError(
            f'image is {w}x{h}px; the per-side limit is {max_side}px')
    if w * h > max_px:
        raise SecurityError(
            f'image has {w * h / 1e6:.0f} MP; the limit is '
            f'{max_px / 1e6:.0f} MP (decompression bomb guard)')


# --------------------------------------------------------------------------
# 2. PDF active-content scan
# --------------------------------------------------------------------------
_PDF_RISK_TOKENS = (
    (b'/JavaScript', 'JavaScript'),
    (b'/JS', 'JavaScript action'),
    (b'/Launch', 'Launch action (starts external programs)'),
    (b'/OpenAction', 'automatic open action'),
    (b'/AA', 'additional actions'),
    (b'/EmbeddedFile', 'embedded file'),
    (b'/RichMedia', 'rich media'),
    (b'/XFA', 'XFA form scripting'),
)


def scan_pdf_risks(path: str, config: Optional[Dict] = None) -> List[str]:
    """
    Report active-content markers inside a PDF. Informational only: pages
    are rasterised to pixels, so none of these can ever execute here - but
    the user deserves to know the file carries them.
    """
    cfg = config or CONFIG
    cap = cfg.get('pdf_scan_bytes', 8 * 1024 * 1024)
    with open(path, 'rb') as f:
        blob = f.read(cap)
    found = []
    for token, label in _PDF_RISK_TOKENS:
        # match the token as a PDF name (not a prefix of a longer name)
        if re.search(re.escape(token) + rb'(?![A-Za-z])', blob):
            found.append(f'PDF contains {label} - it will NOT be executed; '
                         'pages are rasterised to pixels only')
    return found


# --------------------------------------------------------------------------
# 3. decoded-text sanitizing (terminal & file safety)
# --------------------------------------------------------------------------
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]')


def sanitize_text(text: str) -> str:
    """
    Make untrusted decoded text safe to print to a terminal or write to a
    text file: ANSI escape sequences are removed, every other control or
    format character (except newline and tab) becomes a visible escape
    like \\x07. The text content itself is preserved.
    """
    text = _ANSI_RE.sub('', text)
    out = []
    for ch in text:
        if ch in '\n\t':
            out.append(ch)
        elif unicodedata.category(ch) in ('Cc', 'Cf') or ch == '\x7f':
            out.append(f'\\x{ord(ch):02x}')
        else:
            out.append(ch)
    return ''.join(out)


# --------------------------------------------------------------------------
# 4. code detection in decoded content
# --------------------------------------------------------------------------
# Patterns that indicate the hidden message is itself code. Finding one is
# not an error - the text is rendered as inert pixels/plaintext - but it is
# flagged so nobody downstream is tempted to treat it as instructions.
_CODE_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r'^#!\s*/', 'shebang script header'),
    (r'\beval\s*\(', 'eval() call'),
    (r'\bexec\s*\(', 'exec() call'),
    (r'\bimport\s+os\b|\bimport\s+subprocess\b', 'python system import'),
    (r'\bos\.system\s*\(|\bsubprocess\.', 'python process spawning'),
    (r'\brm\s+-rf?\b', 'destructive shell command'),
    (r'\bsudo\s+\w+', 'sudo command'),
    (r'\bcurl\b.{0,40}\|\s*(?:ba)?sh\b', 'curl-pipe-to-shell'),
    (r'\bwget\b.{0,40}\|\s*(?:ba)?sh\b', 'wget-pipe-to-shell'),
    (r'\bpowershell\b|\bInvoke-Expression\b|\bIEX\b', 'powershell'),
    (r'\bcmd(?:\.exe)?\s*/c\b', 'windows shell command'),
    (r'<script\b', 'html script tag'),
    (r'\bjavascript\s*:', 'javascript: url'),
    (r'\bon(?:load|click|error)\s*=', 'html event handler'),
    (r'\bDROP\s+TABLE\b|\bUNION\s+SELECT\b|;\s*--\s*$', 'sql injection'),
    (r'\$\(\s*\)\s*\{.*\}\s*;', 'shellshock-style function'),
    (r'`[^`]{3,}`|\$\([^)]{3,}\)', 'shell command substitution'),
    (r'\bbase64\s+(-d|--decode)\b', 'base64 decode chain'),
    (r'\bchmod\s+\+x\b', 'make-executable command'),
)
_CODE_RES = [(re.compile(p, re.IGNORECASE | re.MULTILINE), label)
             for p, label in _CODE_PATTERNS]


def detect_code(text: str) -> List[str]:
    """Labels of code-like constructs found in decoded text (deduplicated,
    order preserved). Empty list = nothing suspicious."""
    found: List[str] = []
    for rx, label in _CODE_RES:
        if rx.search(text) and label not in found:
            found.append(label)
    return found


def security_report(text: str) -> Dict:
    """Bundle of all decoded-content checks, attached to pipeline results."""
    findings = detect_code(text)
    return {
        'code_suspect': bool(findings),
        'findings': findings,
        'note': ('decoded content is rendered as plain text only; '
                 'it is never evaluated or executed'),
    }


# --------------------------------------------------------------------------
# 5. output path safety
# --------------------------------------------------------------------------
_SAFE_NAME_RE = re.compile(r'[^A-Za-z0-9._-]+')


def safe_basename(path: str, max_len: int = 80) -> str:
    """A filesystem-safe stem derived from an untrusted input filename:
    no directories, no separators, no control characters, bounded length,
    never empty and never a dot-file."""
    stem = os.path.splitext(os.path.basename(path or ''))[0]
    stem = _SAFE_NAME_RE.sub('_', stem).strip('._')
    return (stem or 'image')[:max_len]


def safe_output_path(out_dir: str, filename: str) -> str:
    """Join and verify that the result stays inside out_dir."""
    final = os.path.realpath(os.path.join(out_dir, filename))
    root = os.path.realpath(out_dir)
    if not (final == root or final.startswith(root + os.sep)):
        raise SecurityError('output path escapes the output directory')
    return final
