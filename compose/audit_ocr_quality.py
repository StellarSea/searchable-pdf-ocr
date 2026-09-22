"""Accuracy signals for a finished searchable PDF that need no manual transcription.

Neither check proves the text is right. They bound the problem from two sides and
name the pages worth looking at, which is what is available without a human
transcribing ground truth.

agreement
    Two independent models read the same pixels: the document VLM produced the
    paragraph text, and the small line recognizer re-read every line during
    alignment. Their character-level agreement is recorded per block in the line
    cache. High agreement does not mean both are right, and both can fail the
    same way on the same bad scan, so read a low rate as "look here", not as an
    error rate.

coverage
    Needs nothing known about the book, so this is the check for an arbitrary
    scan. It subtracts the detected layout blocks from the page's own ink and
    reports what is left over, which is how a region the document model never
    detected becomes visible. No later stage can see that, because they all
    start from the blocks that were found.

sequence
    A workbook numbers its items. Every number that the source must contain and
    the extracted text does not is a real defect: text was lost, mis-read, or
    never assigned. This finds errors outright rather than estimating them.
    Numbers found out of order are usually multi-column reading order, not a
    recognition fault, so they are counted separately.
"""
import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pymupdf as fitz

from ocr_schema import _find_size, _norm_bbox
from ocr_storage import atomic_json
from ocr_artifacts import resolve_text
from ocr_text import strip_html
import ocr_workflow as workflow

ITEM_RE = re.compile(r'^[ \t]*(\d{1,3})[.)]\s', re.M)


def agreement(cache, decisions=None):
    """Character agreement between the VLM paragraph text and the line recognizer."""
    if decisions is None:
        with closing(sqlite3.connect(cache)) as db:
            rows = [json.loads(value) for (value,) in db.execute('select value from decisions')]
    else:
        rows = decisions
    dense = lambda text: ''.join(strip_html(text).split())
    buckets, pages = Counter(), {}
    scored = []
    for row in rows:
        before, after = dense(row['original']), dense(''.join(row['candidate']))
        if not before:
            continue
        if not after:
            buckets['no recognizer output'] += 1
            continue
        ratio = (1.0 if before == after else
                 SequenceMatcher(None, before, after, autojunk=False).ratio())
        scored.append((ratio, row['page'], len(before)))
        band = ('0.99+' if ratio >= 0.99 else '0.95-0.99' if ratio >= 0.95 else
                '0.90-0.95' if ratio >= 0.90 else 'under 0.90')
        buckets[band] += 1
        pages.setdefault(row['page'], []).append(ratio)
    weighted = (sum(r*n for r, _, n in scored) / sum(n for _, _, n in scored)) if scored else 0.0
    worst = sorted((min(v), p) for p, v in pages.items())[:12]
    return {'blocks': len(rows), 'compared': len(scored),
            'character_weighted_agreement': round(weighted, 4),
            'bands': dict(buckets),
            'worst_pages': [{'page': p, 'lowest_block_agreement': round(r, 3)} for r, p in worst]}


def coverage(pdf, pruned=None, zoom=0.5, edge=0.02, floor=0.12, contrast=40):
    """Find ink the layout analysis never covered with a block.

    This is the failure that hides on an arbitrary book: a sidebar, a second
    column, a caption or a stamp that the document model simply did not detect.
    Nothing downstream can notice, because every later stage starts from the
    blocks that were found -- the pipeline's own character check compares the PDF
    against the OCR text, not against the page. So the page pixels are consulted
    directly here and the detected blocks are subtracted from them.

    Ink is measured as local contrast rather than darkness, so a page printed on
    a dark background does not read as ink from edge to edge. Ink within `edge`
    of the border is ignored, which is where book scanning leaves dark margins.
    A page is reported when more than `floor` of its ink falls outside every
    block. Pictures do not trigger it: a photograph sits inside a detected image
    block, so its ink counts as covered.
    """
    pages = json.loads(resolve_text(pruned).read_text(encoding='utf-8')) if pruned else None
    rows, flagged = [], []
    with fitz.open(pdf) as doc:
        for index in range(len(doc)):
            page = doc[index]
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY,
                                  alpha=False)
            # Glyphs are local contrast, not darkness: a page printed on a dark
            # background is not ink from edge to edge, and counting it as such
            # reported every coloured page as a missed region.
            gray = (np.frombuffer(pix.samples, dtype=np.uint8)
                    .reshape(pix.height, pix.width).astype(np.int16))
            mask = np.zeros(gray.shape, dtype=bool)
            mask[:, :-1] |= np.abs(np.diff(gray, axis=1)) > contrast
            mask[:-1, :] |= np.abs(np.diff(gray, axis=0)) > contrast
            margin_y, margin_x = int(pix.height*edge), int(pix.width*edge)
            if margin_y and margin_x:
                mask[:margin_y], mask[-margin_y:] = False, False
                mask[:, :margin_x], mask[:, -margin_x:] = False, False
            total = int(mask.sum())
            row = {'page': index + 1, 'ink': total,
                   'characters': len(''.join(page.get_text().split()))}
            if pages is not None and total:
                blocks = pages[index].get('parsing_res_list', []) if index < len(pages) else []
                size = _find_size(pages[index]) if blocks else None
                if size and size[0] and size[1]:
                    sx = page.rect.width / size[0] * zoom
                    sy = page.rect.height / size[1] * zoom
                    for block in blocks:
                        box = _norm_bbox(block.get('block_bbox'))
                        if not box:
                            continue
                        x0 = max(0, int(box[0]*sx) - 2); x1 = min(pix.width, int(box[2]*sx) + 3)
                        y0 = max(0, int(box[1]*sy) - 2); y1 = min(pix.height, int(box[3]*sy) + 3)
                        if x1 > x0 and y1 > y0:
                            mask[y0:y1, x0:x1] = False
                outside = int(mask.sum())
                row['ink_outside_blocks'] = outside
                row['fraction_outside'] = round(outside/total, 3)
                row['blocks'] = len(blocks)
                if total > 200 and outside/total > floor:
                    flagged.append(row)
            rows.append(row)
    flagged.sort(key=lambda r: -r['fraction_outside'])
    return {'pages': len(rows),
            'pages_without_any_text': [r['page'] for r in rows if not r['characters']],
            'checked_against_layout': pages is not None,
            'pages_with_uncovered_ink': [
                {k: r[k] for k in ('page', 'fraction_outside', 'ink', 'blocks', 'characters')}
                for r in flagged[:40]],
            'uncovered_page_count': len(flagged)}


def sequence(pdf, first, last, copies):
    """Check each item number appears as often as the book must contain it.

    Counting occurrences rather than segmenting the document into runs is what
    makes this reliable: a page can carry unrelated numbering (a form printed as
    a question's graphic numbers its own fields 1-4), which defeats any attempt
    to cut the stream into runs but cannot hide a number that is genuinely gone.
    Extra occurrences are reported but are not failures; a shortfall is.
    """
    with fitz.open(pdf) as doc:
        pages_of = {}
        for index in range(len(doc)):
            for match in ITEM_RE.finditer(doc[index].get_text()):
                pages_of.setdefault(int(match.group(1)), []).append(index + 1)
    short, extra = [], []
    for number in range(first, last + 1):
        seen = pages_of.get(number, [])
        if len(seen) < copies:
            short.append({'item': number, 'found': len(seen), 'expected': copies,
                          'pages': seen})
        elif len(seen) > copies:
            extra.append({'item': number, 'found': len(seen)})
    return {'items': f'{first}-{last}', 'copies_expected': copies,
            'items_short': short, 'items_with_extra_occurrences': extra,
            'total_shortfall': sum(c['expected'] - c['found'] for c in short)}


def layout_cache(pdf, stem):
    """Find the layout cache this PDF was actually built from.

    When a rerun's source or API settings no longer match, the pipeline keeps
    the old cache and writes a verified one under .ocr_cache/<fingerprint>/.
    Reading the stale file instead reports the previous run's layout, which is
    exactly the mistake that made a fixed book look unchanged.
    """
    report = workflow.find_artifact(pdf.parent, f'{stem}_auto_report.json')
    try:
        recorded = json.loads(report.read_text(encoding='utf-8'))
        if recorded.get('layout_cache') and resolve_text(recorded['layout_cache']).is_file():
            return resolve_text(recorded['layout_cache'])
        identity = recorded['source_identity']
        fingerprint = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
        isolated = resolve_text(workflow.find_artifact(pdf.parent, '.ocr_cache') / fingerprint / 'pruned.json')
        if isolated.exists():
            return isolated
    except (ValueError, OSError, KeyError):
        pass
    return workflow.find_artifact(pdf.parent, f'{stem}_pruned.json')


def audit_one(pdf, cache=None, pruned=None, items=None, copies=1):
    explicit_cache = cache is not None
    stem = pdf.name.split('_auto_')[0]
    pruned = pruned or layout_cache(pdf, stem)
    cache = cache or workflow.find_artifact(pdf.parent, f'{stem}_line_ocr.sqlite3')
    status_path = workflow.find_artifact(pdf.parent, f'{stem}_auto_status.json')
    report = {'pdf': str(pdf), 'document': stem,
              'coverage': coverage(pdf, pruned if resolve_text(pruned).exists() else None)}
    if items:
        first, last = (int(v) for v in items.split('-'))
        report['sequence'] = sequence(pdf, first, last, copies)
    try:
        saved = json.loads(workflow.find_artifact(pdf.parent, f'{stem}_auto_report.json').read_text(encoding='utf-8'))
        decisions = None if explicit_cache else saved.get('line_decisions')
    except (ValueError, OSError):
        decisions = None
    report['agreement'] = (agreement(cache, decisions) if decisions is not None or Path(cache).exists()
                           else {'skipped': f'no line cache at {cache}'})
    try:
        report['pipeline_status'] = json.loads(
            status_path.read_text(encoding='utf-8')).get('status')
    except (ValueError, OSError):
        report['pipeline_status'] = None
    return report


def print_one(report, items=None, copies=1):
    cov = report['coverage']
    print(f"pages: {cov['pages']}, with no text at all "
          f"{len(cov['pages_without_any_text'])} {cov['pages_without_any_text'][:12]}")
    if cov['checked_against_layout']:
        print(f"pages with ink outside every detected block: {cov['uncovered_page_count']}")
        for row in cov['pages_with_uncovered_ink'][:12]:
            print(f"  p{row['page']}: {row['fraction_outside']*100:.0f}% of ink uncovered"
                  f"  ({row['blocks']} blocks, {row['characters']} chars)")
    else:
        print('layout cache not found; skipped the uncovered-ink check')
    if 'sequence' in report:
        summary = report['sequence']
        first, last = (int(v) for v in items.split('-'))
        total = (last - first + 1) * copies
        print(f"numbered items {first}-{last} x{copies}: "
              f"{total - summary['total_shortfall']}/{total} occurrences found")
        for short in summary['items_short']:
            print(f"  item {short['item']}: found {short['found']} of {short['expected']}"
                  f"  pages {short['pages']}")
        if not summary['items_short']:
            print('  every expected item number is present')
    a = report['agreement']
    if 'character_weighted_agreement' in a:
        print(f"two-model character agreement: {a['character_weighted_agreement']:.3f} "
              f"over {a['compared']} blocks   {a['bands']}")
        print('lowest-agreement pages: '
              + ', '.join(str(p['page']) for p in a['worst_pages']))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pdf', type=Path,
                    help='a finished *_auto_searchable.pdf, or a folder holding several')
    ap.add_argument('--cache', type=Path, help='matching *_line_ocr.sqlite3 (default: alongside)')
    ap.add_argument('--pruned', type=Path, help='matching *_pruned.json (default: alongside)')
    ap.add_argument('--items', default=None,
                    help='numbered item range each copy must contain, e.g. 1-100; '
                         'omit for a book with no numbered items')
    ap.add_argument('--copies', type=int, default=1,
                    help='how many times the book repeats the item range (e.g. 10 tests)')
    ap.add_argument('--out', type=Path, help='write the report as JSON here')
    args = ap.parse_args()

    if args.pdf.is_dir():
        found = sorted(args.pdf.glob('*_auto_searchable.pdf'))
        if not found:
            sys.exit(f'No finished searchable PDF in {args.pdf}')
        reports = []
        for pdf in found:
            print()
            print(f'=== {pdf.name}')
            report = audit_one(pdf, items=args.items, copies=args.copies)
            print_one(report, args.items, args.copies)
            reports.append(report)
        print()
        print(f'{"document":44s} {"pages":>6s} {"blank":>6s} {"uncov":>6s} '
              f'{"agree":>6s} {"short":>6s}  status')
        for r in reports:
            cov, a = r['coverage'], r['agreement']
            agree = a.get('character_weighted_agreement')
            short = r.get('sequence', {}).get('total_shortfall')
            print(f"{r['document'][:44]:44s} {cov['pages']:6d} "
                  f"{len(cov['pages_without_any_text']):6d} "
                  f"{cov.get('uncovered_page_count', 0):6d} "
                  f"{(f'{agree:.3f}' if agree is not None else '-'):>6s} "
                  f"{(str(short) if short is not None else '-'):>6s}  "
                  f"{r.get('pipeline_status')}")
        report = {'documents': reports}
    else:
        report = audit_one(args.pdf, args.cache, args.pruned, args.items, args.copies)
        print_one(report, args.items, args.copies)

    if args.out:
        atomic_json(args.out, report)
        print(f'[report] {args.out}')


if __name__ == '__main__':
    main()
