"""Offline real-PDF repair A/B: exact crop/box/checkpoint checks, alternating order.

Reads a completed baseline SQLite database in read-only mode. Replays its audited
repair candidates in block order; never starts OCR or writes the operational DB.
Timing includes all real detection / PNG preparation. --live additionally uses
the current OCR endpoint; --overlap compares serial baseline with the new feed.
--rule-runs isolates the long-run implementation with raster reuse on both sides.
"""
import argparse
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def run(source, original, candidates, reuse, *, prefetch=False, live=False, cpu_workers=0):
    pages = copy.deepcopy(original)
    images, boxes, checkpoints, responses = [], [], [], []
    cpu_stats = {}

    def recognize(png, timeout=None):
        index = len(images)
        images.append(hashlib.sha256(png).hexdigest())
        if index >= len(candidates):
            raise AssertionError('Unexpected additional repair request')
        result = ocr.ocr_image(png, timeout=timeout) if live else candidates[index]
        responses.append(result)
        return result

    def detected(page, rect, found):
        boxes.append([page.number, list(rect), [list(box) for box in found]])

    started = time.perf_counter()
    fixed = ocr.repair_blocks(source, pages, reuse_raster=reuse,
                             recognize_image=recognize, on_update=lambda: checkpoints.append(digest(pages)),
                             prefetch=prefetch, cpu_workers=cpu_workers, cpu_stats=cpu_stats,
                             on_detection=detected)
    seconds = time.perf_counter() - started
    if len(images) != len(candidates):
        raise AssertionError(f'Expected {len(candidates)} crops, got {len(images)}')
    exact = {'pages': digest(pages), 'boxes': digest(boxes), 'images': images,
             'checkpoints': checkpoints, 'fixed': fixed}
    return {'reuse_raster': reuse, 'prefetch': prefetch, 'seconds': seconds,
            'detections': len(boxes), 'exact': exact, 'responses': responses, 'cpu': cpu_stats}


def load_replay(source, baseline):
    """Restore recorded inputs without opening an operational database for writes."""
    db = baseline / '.ocr' / f'{source.stem}_line_ocr.sqlite3'
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        def artifact(suffix):
            row = conn.execute('SELECT content FROM artifacts WHERE name=?',
                               (source.stem + suffix,)).fetchone()
            if row is None:
                raise ValueError(f'Missing artifact {suffix}')
            return json.loads(row[0])
        report = artifact('_auto_report.json')
        original = artifact('_pruned.json')
    if hashlib.sha256(source.read_bytes()).hexdigest() != report['source_identity']['sha256']:
        raise ValueError('Baseline/source identity mismatch')
    candidates = []
    for page in original:
        for block in page.get('parsing_res_list', []):
            if 'repair_error' in block:
                raise ValueError('Cannot replay a baseline with failed repairs')
            audit = block.pop('repair_audit', None)
            if audit is not None:
                candidates.append(audit['candidate'])
                block['block_content'] = audit['original']
    if not candidates:
        raise ValueError('Baseline has no repair requests to compare')
    return original, candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--overlap', action='store_true', help='Enable repair prefetch on the candidate')
    parser.add_argument('--live', action='store_true', help='Send actual crop requests to the current OCR service')
    parser.add_argument('--rule-runs', action='store_true',
                        help='Compare exact long-run algorithms with identical raster reuse; offline only')
    args = parser.parse_args()
    if args.rule_runs and (args.live or args.overlap):
        parser.error('--rule-runs cannot be combined with --live or --overlap')
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    original, candidates = load_replay(args.source, args.baseline)
    args.out.mkdir(parents=True, exist_ok=False)
    records = []
    for repeat in range(args.repeats):
        for reuse in ((False, True) if repeat % 2 == 0 else (True, False)):
            label = ('candidate' if reuse else 'reference') if args.rule_runs else f'reuse={reuse}'
            print(f'[benchmark-start] {label}, overlap={args.overlap and reuse}, live={args.live}', flush=True)
            if args.rule_runs:
                import ocr_render
                from benchmark_rule_runs import candidate, reference
                implementation = candidate if reuse else reference
                with patch.object(ocr_render, '_long_runs', implementation):
                    record = run(args.source, original, candidates, True)
                record['implementation'] = implementation.__name__
            else:
                record = run(args.source, original, candidates, reuse, prefetch=args.overlap and reuse, live=args.live)
            record['candidate'] = reuse
            ocr.atomic_json(args.out / f'run-{len(records)+1}.json', record)
            if records and any(record['exact'][key] != records[0]['exact'][key] for key in ('boxes', 'images')):
                raise AssertionError('Repair request pixels or detection coordinates changed')
            records.append(record)
            print(f"[benchmark] {label}: {record['seconds']:.3f}s", flush=True)
    medians = {str(reuse): statistics.median(r['seconds'] for r in records if r['candidate'] == reuse)
               for reuse in (False, True)}
    identical = all(record['exact'] == records[0]['exact'] for record in records)
    result = {'pages': len(original), 'requests': len(candidates), 'records': records,
              'rule_runs_only': args.rule_runs,
              'median_seconds': medians, 'exact_repair_inputs': True,
              'exact_repair_inputs_and_decisions': identical,
              'scope': ('Repair preparation + live OCR only; not whole-PDF throughput' if args.live else
                        'CPU repair preparation only; recorded OCR candidates; no new recognition accuracy claim')}
    ocr.atomic_json(args.out / 'comparison.json', result)
    print(json.dumps({k: v for k, v in result.items() if k != 'records'}), flush=True)
    if not identical:
        raise AssertionError('OCR responses/decisions differ; inspect saved runs (live model variance is not excluded)')


if __name__ == '__main__':
    main()
