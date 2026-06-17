"""
main.py - Entry point for DrawMeBinary.

Reads binary (0/1) text hidden inside artwork and re-renders it as a clean
centred layout (basic mode) or a scattered typographic poster (poster mode),
keeping the original background.

Supports:
  - JPEG / PNG / BMP / TIFF images
  - PDF files - each page is rasterised to a PNG and processed in turn
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

from config import CONFIG, PRESETS, get_config_for_preset
from decoding import LanguageValidator
from pipeline import run_pipeline
from rendering import render_basic_mode, render_poster_mode, get_timestamp
from security import (SecurityError, safe_basename, safe_output_path,
                      sanitize_text, validate_input_file)


def _pdf_to_images(pdf_path: str, dpi: int = 150,
                   max_pages: int = 50) -> list:
    """Rasterise PDF pages (up to max_pages) to temporary PNGs.
    Rasterising draws pixels only: no PDF script or action can run."""
    try:
        import fitz  # pymupdf
        doc = fitz.open(pdf_path)
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        tmp = tempfile.mkdtemp(prefix='drawmebinary_pdf_')
        paths = []
        if len(doc) > max_pages:
            print(f'   PDF has {len(doc)} pages; processing the '
                  f'first {max_pages} (max_pdf_pages)')
        for n in range(min(len(doc), max_pages)):
            pix = doc[n].get_pixmap(matrix=mat, alpha=False)
            out = os.path.join(tmp, f'page_{n + 1:04d}.png')
            pix.save(out)
            paths.append(out)
        doc.close()
        print(f'   PDF: {len(paths)} page(s) extracted via pymupdf')
        return paths
    except ImportError:
        pass
    try:
        from pdf2image import convert_from_path
        pages = convert_from_path(pdf_path, dpi=dpi)[:max_pages]
        tmp = tempfile.mkdtemp(prefix='drawmebinary_pdf_')
        paths = []
        for i, page in enumerate(pages):
            out = os.path.join(tmp, f'page_{i + 1:04d}.png')
            page.save(out, 'PNG')
            paths.append(out)
        print(f'   PDF: {len(paths)} page(s) extracted via pdf2image')
        return paths
    except ImportError:
        print('No PDF library found. Install either:\n'
              '    pip install pymupdf\n'
              '    pip install pdf2image   (also requires poppler)')
        sys.exit(1)


def save_extracted_text(data: dict, config: dict, image_path: str) -> None:
    # text dumps live in output/text/ so output/ itself stays images-only.
    # Decoded text is untrusted: filenames are sanitized and the text is
    # made terminal/file-safe before it touches the disk.
    out_dir = os.path.join(config.get('output_dir', 'output'), 'text')
    os.makedirs(out_dir, exist_ok=True)
    base = safe_basename(image_path or 'output')
    ts = get_timestamp()
    bits = [g for g in data.get('all_bits', []) if g.get('kind') == 'bin']
    if bits:
        bits.sort(key=lambda g: (g['stream'], g['y'], g['x']))
        path = safe_output_path(out_dir, f'{base}_binary_{ts}.txt')
        with open(path, 'w') as f:
            f.write(''.join(g['bit'] for g in bits))
        print(f'   Binary saved: {path}')
    if data.get('text'):
        path = safe_output_path(out_dir, f'{base}_text_{ts}.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(sanitize_text(data['text']))
        print(f'   Text saved: {path}')


def process_image(image_path: str, args, config: dict, validator,
                  pre_validated: bool = False) -> None:
    print('=' * 70)
    print(f'Image: {os.path.basename(image_path)}')
    print('=' * 70)
    if not pre_validated:
        report = validate_input_file(image_path, config)
        for w in report['warnings']:
            print(f'   WARNING: {w}')
    results = run_pipeline(image_path, config, validator, verbose=True)
    if args.save_text:
        save_extracted_text(results, config, image_path)
    if args.poster:
        render_poster_mode(results, config, validator, image_path,
                           img=results.get('img'))
    else:
        render_basic_mode(results, config, image_path,
                          img=results.get('img'))


def main() -> None:
    parser = argparse.ArgumentParser(
        description='DrawMeBinary - binary OCR text extraction & re-render')
    parser.add_argument('image', nargs='?',
                        help='Path to an image, a PDF, or a folder of '
                             'images (every image inside is processed)')
    parser.add_argument('-b', '--basic', action='store_true',
                        help='clean centred layout (default)')
    parser.add_argument('-p', '--poster', action='store_true',
                        help='scattered typographic poster')
    parser.add_argument('--preset', type=str, choices=list(PRESETS.keys()))
    parser.add_argument('--pdf-dpi', type=int, default=150,
                        help='DPI for PDF rasterisation (default: 150)')
    parser.add_argument('--save-text', action='store_true',
                        help='also save extracted bits and text to output/')
    args = parser.parse_args()

    print(f"Mode: {'POSTER' if args.poster else 'BASIC'}\n")
    if not args.image:
        print('Provide an image or PDF path.')
        sys.exit(1)
    if not os.path.exists(args.image):
        # be forgiving about the working directory: try the project root
        # (e.g. `python main.py test -b` run from inside drawmebinary/)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        alt = os.path.join(root, args.image)
        if os.path.exists(alt):
            args.image = alt
        else:
            print(f'File not found: {args.image} (also tried {alt})')
            sys.exit(1)

    try:
        config = get_config_for_preset(args.preset) if args.preset \
            else dict(CONFIG)
        validator = LanguageValidator()

        if os.path.isdir(args.image):
            exts = ('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff')
            images = [os.path.join(args.image, f)
                      for f in sorted(os.listdir(args.image))
                      if f.lower().endswith(exts)]
            if not images:
                print(f'No images found in {args.image}')
                sys.exit(1)
            print(f'Folder input - {len(images)} image(s)\n')
            failed = []
            for i, path in enumerate(images, 1):
                print(f'\n--- Image {i} / {len(images)} ---')
                try:
                    process_image(path, args, config, validator)
                except Exception as exc:
                    failed.append((os.path.basename(path), exc))
                    print(f'   FAILED: {exc}')
            if failed:
                print(f'\n{len(failed)} image(s) failed: '
                      + ', '.join(n for n, _ in failed))
        elif Path(args.image).suffix.lower() == '.pdf':
            report = validate_input_file(args.image, config)
            for w in report['warnings']:
                print(f'WARNING: {w}')
            print(f'PDF input - rasterising pages at {args.pdf_dpi} DPI...')
            pages = _pdf_to_images(args.image, dpi=args.pdf_dpi,
                                   max_pages=config.get('max_pdf_pages',
                                                        50))
            stem = safe_basename(args.image)
            for i, page_path in enumerate(pages, 1):
                print(f'\n--- Page {i} / {len(pages)} ---')
                renamed = os.path.join(os.path.dirname(page_path),
                                       f'{stem}_page{i:04d}.png')
                os.rename(page_path, renamed)
                process_image(renamed, args, config, validator,
                              pre_validated=True)
        else:
            process_image(args.image, args, config, validator)
        print('\nDone!')
    except KeyboardInterrupt:
        print('\nInterrupted')
        sys.exit(1)
    except SecurityError as exc:
        print(f'\nREFUSED: {exc}')
        sys.exit(2)
    except Exception as exc:
        import traceback
        print(f'\nError: {exc}')
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
