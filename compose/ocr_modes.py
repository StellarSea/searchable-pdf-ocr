"""CLI options and output/completion naming. Preserve these names for resume and batch compatibility."""
import argparse
import hashlib
from pathlib import Path
from ocr_artifacts import resolve_text


def auto_tag(args):
    """Name debug status and report files apart from a real run's.

    A border-drawing run is a diagnostic whose output is not the deliverable, so
    it must not overwrite the status and report describing the finished PDF.
    """
    return '_auto_debug' if args.debug_lines else '_auto'

def is_automatic(args):
    return not (args.fast or args.paragraph or args.dump_structure or args.line_ocr_pages)

def run_tag(args):
    if is_automatic(args):
        return auto_tag(args)
    mode = ('structure' if args.dump_structure else 'paragraph' if args.paragraph
            else 'line' if args.line_ocr_pages else 'fast')
    return '_' + mode + ('_debug' if args.debug_lines else '')

def run_options(args):
    options = {name: getattr(args, name) for name in
               ('fast', 'paragraph', 'dump_structure', 'line_ocr_pages',
                'debug_lines', 'toc', 'toc_all', 'no_repair')}
    options['line_review'] = (hashlib.sha256(resolve_text(args.line_ocr_review).read_bytes()).hexdigest()
                              if args.line_ocr_review else None)
    layout_review = getattr(args, 'layout_review', None)
    if layout_review:
        options['layout_review'] = hashlib.sha256(resolve_text(layout_review).read_bytes()).hexdigest()
    if not getattr(args, 'no_boundary_repair', False):
        from ocr_boundary import VERSION
        options['boundary_repair'] = VERSION
    if getattr(args, 'bookmarks', False):
        from ocr_bookmarks import VERSION as BOOKMARK_VERSION
        options['bookmarks'] = BOOKMARK_VERSION
        if getattr(args, 'bookmark_review', None):
            options['bookmark_review'] = hashlib.sha256(resolve_text(args.bookmark_review).read_bytes()).hexdigest()
    return options

def completed_status(args, identity, output, report=None):
    report = report or {}
    return {'status': report.get('status', 'completed'), 'output': str(output),
            'source_identity': identity, 'options': run_options(args),
            'review_pages': report.get('review_pages', [])}

def memory_budget(value):
    number = int(value)
    if number < 256:
        raise argparse.ArgumentTypeError('CPU memory budget must be at least 256 MiB')
    return number


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--batch", type=int, default=10, help="N페이지씩 나눠 보내기 (0=통째로)"
    )
    ap.add_argument('--ocr-requests', type=int, choices=(1, 2), default=2,
                    help='Document OCR HTTP requests in flight (default 2; 1 restores serial mode)')
    ap.add_argument('--bookmarks', action='store_true',
                    help='Match printed contents to body titles and add verified PDF bookmarks; overrides --toc/--toc-all')
    ap.add_argument('--bookmark-review', type=Path,
                    help='Source/layout-bound, visually verified bookmark-only title and folio corrections')
    ap.add_argument("--toc", action="store_true")
    ap.add_argument("--toc-all", action="store_true")
    ap.add_argument(
        "--paragraph",
        action="store_true",
        help="줄 검출 없이 문단 단위로 삽입 (예전 방식)",
    )
    ap.add_argument(
        "--debug-lines",
        action="store_true",
        help="검출된 줄 상자를 빨간 테두리로 그린다",
    )
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument('--fast', action='store_true', help='Skip automatic full-document line OCR and final verification')
    ap.add_argument('--line-ocr-pages', help='Opt-in crop OCR on these 1-based pages, e.g. 13,17,26 (or all)')
    ap.add_argument('--line-ocr-review', type=Path, help='Explicit image-bound, manually reviewed block corrections')
    ap.add_argument('--layout-review', type=Path,
                    help='Source-bound, visually reviewed text and line/cell coordinates')
    ap.add_argument('--no-boundary-repair', action='store_true',
                    help='Disable automatic clipped-block expansion and re-recognition')
    ap.add_argument('--no-prefetch', action='store_true',
                    help='Disable CPU rendering overlap with repair / line OCR (same recognition settings)')
    ap.add_argument('--cpu-workers', type=int, choices=range(0, 17), default=16,
                    help='Automatic-mode CPU preparation workers (default 16; 0 keeps serial preparation)')
    ap.add_argument('--cpu-memory-mb', type=memory_budget, default=16384,
                    help='CPU preparation memory budget in MiB (default 16384; pressure falls back to serial)')
    ap.add_argument('--verify-workers', type=int, choices=range(0, 17), default=8,
                    help='Final PDF verification workers (default 8; 0 serial; capped by CPU workers and memory)')
    ap.add_argument('--no-gpu-prefetch', action='store_true',
                    help='Disable bounded cross-block GPU request batching (same models and crops)')
    ap.add_argument("--trust-cache", action="store_true", help="Allow legacy caches without source fingerprints")
    ap.add_argument("--repair-cache", action="store_true", help="Recheck cached pages for missing OCR text")
    ap.add_argument(
        "--no-repair",
        action="store_true",
        help="본문이 유실된 블록을 잘라 다시 읽는 단계를 건너뛴다",
    )
    ap.add_argument("--dump-structure", action="store_true")
    return ap
