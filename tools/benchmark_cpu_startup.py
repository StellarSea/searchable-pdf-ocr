"""Compare current overlay with a completed baseline, using its exact OCR layout.

The baseline database/PDF and original source are read-only. The candidate gets
a fresh line/detection cache. This isolates startup scheduling from VLM sampling.
--offline-replay copies only image-response keys, regenerates geometry and PNGs,
and forbids new recognition; it also checks algorithm edits against the baseline.
"""
import argparse
import copy
from contextlib import closing, nullcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_to_searchable_pdf as ocr
from ocr_workflow import verify
from benchmark_cpu_overlay import assert_pdf_equal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--offline-replay', action='store_true',
                        help='Reuse only exact PNG response keys; forbid HTTP and regenerate geometry/crops')
    args = parser.parse_args()
    stem = args.source.stem
    db = args.baseline/'.ocr'/f'{stem}_line_ocr.sqlite3'
    with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro', uri=True)) as conn:
        def artifact(name):
            row = conn.execute('SELECT content FROM artifacts WHERE name=?', (name,)).fetchone()
            if row is None:
                raise ValueError(f'Missing baseline artifact: {name}')
            return json.loads(row[0])
        baseline = artifact(f'{stem}_auto_report.json')
        layout_reference = baseline['layout_cache']
        layout_name = layout_reference.split('::', 1)[1] if '::' in layout_reference else Path(layout_reference).name
        pages = artifact(layout_name)
        responses = dict(conn.execute('SELECT key, value FROM responses')) if args.offline_replay else {}
    sha = hashlib.sha256(args.source.read_bytes()).hexdigest()
    if sha != baseline['source_identity']['sha256']:
        raise ValueError('Baseline belongs to a different source')
    args.out.mkdir(parents=True, exist_ok=False)
    refiner = ocr.LineRefiner(args.out/'lines.json', set(range(1, len(pages)+1)),
                             automatic=True, source_sha256=sha)
    try:
        if args.offline_replay:
            refiner.store.save_responses(responses)
        started = time.perf_counter()
        guard = patch.object(ocr, 'recognize_lines', side_effect=AssertionError('Replay must remain offline'))
        with guard if args.offline_replay else nullcontext() as api:
            report = ocr.overlay(args.source, pages, args.out/'output.pdf', automatic=True,
                line_refiner=refiner, cpu_workers=16, gpu_prefetch=True)
            if args.offline_replay and api.call_count:
                raise AssertionError('Exact PNG cache did not cover all requests')
        overlay_seconds = time.perf_counter()-started
        checked = verify(args.source, args.out/'output.pdf', copy.deepcopy(report))
        total_seconds = time.perf_counter()-started
        assert_pdf_equal(args.baseline/f'{stem}_auto_searchable.pdf', args.out/'output.pdf')
        if checked['pages'] != baseline['pages'] or refiner.cache['decisions'] != baseline['line_decisions']:
            raise AssertionError('Quality decisions changed')
        if checked['validation_failed']:
            raise AssertionError('Output verification failed')
        result = {'overlay_seconds': overlay_seconds, 'verify_seconds': total_seconds-overlay_seconds,
                  'total_seconds': total_seconds, 'exact_pixels_text_coordinates_decisions': True,
                  'offline_replay': args.offline_replay,
                  'cpu': report['cpu_prepare_stats'], 'gpu': report['gpu_feed_stats']}
        (args.out/'comparison.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)
    finally:
        refiner.close()


if __name__ == '__main__':
    main()
