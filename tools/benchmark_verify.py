"""Compare the saved pre-change verifier with bounded process verification.

Rebuilds the expected-character report using copied warm OCR caches, never using
the output PDF as its own expected text. All network calls are forbidden. Only
new diagnostic files are written; operational PDF / DBs remain read-only.
"""
import argparse
from contextlib import closing
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from unittest.mock import patch
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_to_searchable_pdf as ocr
import ocr_verify
from benchmark_cpu_overlay import assert_pdf_equal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--baseline-dir', type=Path, required=True)
    parser.add_argument('--cache-db', type=Path, required=True)
    parser.add_argument('--baseline-module', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, nargs='+', default=[0, 2, 4, 8, 16])
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--repeat-pages', type=int, default=1,
                        help='Repeat the same real pages for worker lifecycle stress; not broader accuracy coverage')
    args = parser.parse_args()
    if args.repeats < 1 or args.repeat_pages < 1 or any(n < 0 or n > 16 for n in args.workers):
        parser.error('Invalid repeats/worker count')
    spec = importlib.util.spec_from_file_location('previous_verifier', args.baseline_module)
    baseline_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline_module)
    source_sha = hashlib.sha256(args.source.read_bytes()).hexdigest()
    stem = args.source.stem
    database = args.baseline_dir/'.ocr'/f'{stem}_line_ocr.sqlite3'
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as conn:
        def artifact(name):
            row = conn.execute('SELECT content FROM artifacts WHERE name=?', (name,)).fetchone()
            if row is None:
                raise ValueError(f'Missing baseline artifact: {name}')
            return json.loads(row[0])
        baseline_report = artifact(f'{stem}_auto_report.json')
        if baseline_report['source_identity']['sha256'] != source_sha:
            raise ValueError('Baseline belongs to another source')
        reference = baseline_report['layout_cache']
        layout = artifact(reference.split('::', 1)[1] if '::' in reference else Path(reference).name)
    args.out.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(args.cache_db.resolve().as_uri()+'?mode=ro', uri=True)) as original, \
         closing(sqlite3.connect(args.out/'lines.sqlite3')) as candidate:
        original.backup(candidate)
    refiner = ocr.LineRefiner(args.out/'lines.json', set(range(1, len(layout)+1)),
                             automatic=True, source_sha256=source_sha)
    try:
        with patch.object(ocr, 'recognize_lines', side_effect=AssertionError('Benchmark must remain offline')) as api:
            report = ocr.overlay(args.source, layout, args.out/'output.pdf', automatic=True,
                line_refiner=refiner, cpu_workers=0, gpu_prefetch=False, prefetch=False)
            if api.call_count:
                raise AssertionError('Copied warm cache did not cover every line request')
    finally:
        refiner.close()
    assert_pdf_equal(args.baseline_dir/f'{stem}_auto_searchable.pdf', args.out/'output.pdf')
    verify_source, verify_output = args.source, args.out/'output.pdf'
    reference_pages = copy.deepcopy(baseline_report['pages'])
    if args.repeat_pages > 1:
        with fitz.open(verify_source) as before, fitz.open(verify_output) as after, \
             fitz.open() as long_source, fitz.open() as long_output:
            for repeat in range(args.repeat_pages):
                long_source.insert_pdf(before, final=repeat == args.repeat_pages-1)
                long_output.insert_pdf(after, final=repeat == args.repeat_pages-1)
            verify_source, verify_output = args.out/'long-source.pdf', args.out/'long-output.pdf'
            long_source.save(verify_source)
            long_output.save(verify_output)
        report['pages'] = [copy.deepcopy(page) for _ in range(args.repeat_pages) for page in report['pages']]
        reference_pages = [copy.deepcopy(page) for _ in range(args.repeat_pages) for page in reference_pages]
        for index, (page, reference_page) in enumerate(zip(report['pages'], reference_pages)):
            page['page'] = reference_page['page'] = index+1
    ocr.atomic_json(args.out/'expected-report.json', report)
    records, expected = [], None
    order = ['baseline', *args.workers]
    for repeat in range(args.repeats):
        for variant in order if repeat % 2 == 0 else reversed(order):
            print(f'[verify-benchmark-start] {variant}', flush=True)
            checked, stats = copy.deepcopy(report), {}
            started = time.perf_counter()
            if variant == 'baseline':
                baseline_module.verify(verify_source, verify_output, checked)
            else:
                ocr_verify.verify(verify_source, verify_output, checked, workers=variant, stats=stats)
            seconds = time.perf_counter()-started
            if expected is None:
                expected = checked
                if checked['pages'] != reference_pages or checked['validation_failed']:
                    raise AssertionError('Warm reconstruction differs from completed baseline findings')
            if checked != expected:
                raise AssertionError('Verification findings changed')
            record = {'variant': variant, 'seconds': seconds, 'stats': stats, 'exact_report': True}
            records.append(record)
            ocr.atomic_json(args.out/f'run-{len(records)}.json', record)
            print(f'[verify-benchmark] {variant}: {seconds:.3f}s', flush=True)
    medians = {str(variant): statistics.median(r['seconds'] for r in records if r['variant'] == variant)
               for variant in order}
    result = {'pages': len(report['pages']), 'unique_sample_pages': len(layout),
              'records': records, 'median_seconds': medians,
              'exact_reports_and_reference_pdf': True, 'scope': 'Final verification only; no OCR/model changes'}
    ocr.atomic_json(args.out/'comparison.json', result)
    print(json.dumps({k: v for k, v in result.items() if k != 'records'}), flush=True)


if __name__ == '__main__':
    main()
