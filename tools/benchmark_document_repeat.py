"""Repeat byte-identical input against the existing OCR service, without caches.

Explicit --live is required. Only a new diagnostic directory is written. Results
measure repeatability, not ground-truth accuracy. No service/config changes.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
from ocr_api import API, API_OPTIONS, _call_api
from ocr_source import split_pdf
from ocr_storage import atomic_json


def snapshot(result, expected):
    rows = result.get('layoutParsingResults')
    if not isinstance(rows, list) or len(rows) != expected:
        raise ValueError('Unexpected OCR page count')
    pages = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('prunedResult'), dict):
            raise ValueError('Invalid OCR page')
        blocks = row.get('prunedResult', {}).get('parsing_res_list')
        if not isinstance(blocks, list):
            raise ValueError('Missing OCR blocks')
        page = []
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get('block_content'), str):
                raise ValueError('Invalid OCR block text')
            page.append({key: block.get(key) for key in
                         ('block_label', 'block_bbox', 'block_id', 'block_order', 'block_content')})
        pages.append(page)
    return pages


def differences(reference, candidate):
    if len(reference) != len(candidate):
        raise ValueError('Cannot compare shifted pages')
    changed = []
    for page, (before, after) in enumerate(zip(reference, candidate), 1):
        for index in range(max(len(before), len(after))):
            a = before[index] if index < len(before) else None
            b = after[index] if index < len(after) else None
            if a != b:
                keys = sorted(set(a or {}) | set(b or {}))
                changed.append({'page': page, 'block_index': index,
                                'fields': [key for key in keys
                                           if (a or {}).get(key) != (b or {}).get(key)]})
    return changed


def content_changes(reference, candidate, changed):
    """Separate whitespace variation without treating either response as truth."""
    substantive = []
    for item in changed:
        page, index = item['page'] - 1, item['block_index']
        before, after = reference[page], candidate[page]
        if index >= len(before) or index >= len(after):
            substantive.append(item)
        elif ''.join(before[index]['block_content'].split()) != ''.join(after[index]['block_content'].split()):
            substantive.append(item)
    return substantive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--batch', type=int, default=10, help='First PDF chunk size')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--image', action='store_true', help='Read image bytes instead of a PDF chunk')
    parser.add_argument('--no-layout', action='store_true', help='Isolate recognition for an image crop')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live or args.batch < 1 or args.repeats < 2 or (args.no_layout and not args.image):
        parser.error('Require --live, positive --batch, repeats >= 2; --no-layout requires --image')
    if args.image:
        data, expected, file_type = args.source.read_bytes(), 1, 1
    else:
        with closing(split_pdf(args.source, args.batch)) as chunks:
            _, data, total = next(chunks)
        expected, file_type = min(args.batch, total), 0
    options = {'useLayoutDetection': False} if args.no_layout else {}
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / ('request.image' if args.image else 'request.pdf')).write_bytes(data)
    records, reference = [], None
    summary = {'input_sha256': hashlib.sha256(data).hexdigest(), 'input_bytes': len(data),
               'source': str(args.source.resolve()), 'expected_pages': expected,
               'endpoint': API, 'options': {**API_OPTIONS, **options},
               'scope': 'Sequential repeatability, not accuracy; no downstream pipeline or cache',
               'status': 'running', 'records': records}
    atomic_json(args.out / 'comparison.json', summary)
    for index in range(args.repeats):
        print(f'[repeat] {index + 1}/{args.repeats} start', flush=True)
        started = time.perf_counter()
        # Do not hide failed/retried requests inside this diagnostic.
        try:
            result = _call_api(data, file_type, options=options, attempts=1)
        except Exception as error:
            response = getattr(error, 'response', None)
            summary.update(status='failed', error=str(error), failed_run=index + 1,
                           response_error=response.text[:2000] if response is not None else None)
            atomic_json(args.out / 'comparison.json', summary)
            raise
        seconds = time.perf_counter() - started
        atomic_json(args.out / f'response-{index + 1}.json', result)
        pages = snapshot(result, expected)
        if reference is None:
            reference = pages
        changed = differences(reference, pages)
        record = {'run': index + 1, 'seconds': seconds, 'blocks': sum(map(len, pages)),
                  'different_blocks': changed,
                  'non_whitespace_content_changes': content_changes(reference, pages, changed)}
        records.append(record)
        atomic_json(args.out / 'comparison.json', summary)
        print(f'[repeat] {seconds:.3f}s, changed blocks={len(changed)}', flush=True)
    summary.update(status='completed', identical=not any(r['different_blocks'] for r in records))
    atomic_json(args.out / 'comparison.json', summary)
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    return 0 if summary['identical'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
