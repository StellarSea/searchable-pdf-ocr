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

sequence
    A workbook numbers its items. Every number that the source must contain and
    the extracted text does not is a real defect: text was lost, mis-read, or
    never assigned. This finds errors outright rather than estimating them.
    Numbers found out of order are usually multi-column reading order, not a
    recognition fault, so they are counted separately.
"""
import argparse
import json
import re
import sqlite3
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf as fitz

import ocr_to_searchable_pdf as ocr

ITEM_RE = re.compile(r'^[ \t]*(\d{1,3})[.)]\s', re.M)


def agreement(cache):
    """Character agreement between the VLM paragraph text and the line recognizer."""
    with sqlite3.connect(cache) as db:
        rows = [json.loads(value) for (value,) in db.execute('select value from decisions')]
    db.close()
    dense = lambda text: ''.join(ocr.strip_html(text).split())
    buckets, pages = Counter(), {}
    scored = []
    for row in rows:
        before, after = dense(row['original']), dense(''.join(row['candidate']))
        if not before:
            continue
        if not after:
            buckets['no recognizer output'] += 1
            continue
        ratio = SequenceMatcher(None, before, after, autojunk=False).ratio()
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pdf', type=Path, help='the finished *_auto_searchable.pdf')
    ap.add_argument('--cache', type=Path, help='matching *_line_ocr.sqlite3 (default: alongside)')
    ap.add_argument('--items', default='1-100',
                    help='numbered item range each run must contain, e.g. 1-100')
    ap.add_argument('--copies', type=int, default=1,
                    help='how many times the book repeats the item range (e.g. 10 tests)')
    ap.add_argument('--out', type=Path, help='write the report as JSON here')
    args = ap.parse_args()

    first, last = (int(v) for v in args.items.split('-'))
    cache = args.cache
    if cache is None:
        stem = args.pdf.name.split('_auto_')[0]
        cache = args.pdf.parent / f'{stem}_line_ocr.sqlite3'
    report = {'pdf': str(args.pdf),
              'sequence': sequence(args.pdf, first, last, args.copies)}
    if cache.exists():
        report['agreement'] = agreement(cache)
    else:
        report['agreement'] = {'skipped': f'no line cache at {cache}'}

    summary = report['sequence']
    total = (last - first + 1) * args.copies
    print(f"numbered items {first}-{last} x{args.copies}: "
          f"{total - summary['total_shortfall']}/{total} occurrences found")
    for short in summary['items_short']:
        print(f"  item {short['item']}: found {short['found']} of {short['expected']}"
              f"  pages {short['pages']}")
    if not summary['items_short']:
        print('  every expected item number is present')
    if 'character_weighted_agreement' in report['agreement']:
        a = report['agreement']
        print(f"two-model character agreement: {a['character_weighted_agreement']:.3f} "
              f"over {a['compared']} blocks   {a['bands']}")
        print('lowest-agreement pages: '
              + ', '.join(str(p['page']) for p in a['worst_pages']))
    if args.out:
        ocr.atomic_json(args.out, report)
        print(f'[report] {args.out}')


if __name__ == '__main__':
    main()
