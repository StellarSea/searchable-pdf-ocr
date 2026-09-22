"""Paired real-HTTP overlay benchmark; only explicit diagnostic outputs are written.

The source and production OCR caches are read-only. GPU service must already be
healthy. This tool never starts/stops it. Every run gets fresh derived caches.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_to_searchable_pdf as ocr
from ocr_storage import atomic_json


def comparable(report):
    return {k: v for k, v in report.items()
            if k not in ('line_cache_stats', 'detection_cache_stats', 'prefetch_stats', 'cpu_prepare_stats')}


def run(args):
    if args.out.exists():
        raise ValueError('Use a new output directory; cold-cache measurements must not reuse old caches')
    args.out.mkdir(parents=True)
    numbers = [int(n) for n in args.pages.split(',')]
    layouts = json.loads(args.layout.read_text(encoding='utf-8'))
    chosen = [layouts[n-1] if isinstance(layouts, list) else layouts['pages'][str(n-1)]['result'] for n in numbers]
    source = args.out/'source.pdf'
    with fitz.open(args.source) as doc:
        doc.select([n-1 for n in numbers])
        doc.save(source)
    with source.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    reference_report = reference_decisions = reference_path = None
    runs = []
    for round_index in range(args.rounds):
        order = [False, True] if round_index % 2 == 0 else [True, False]
        for enabled in order:
            name = f'{round_index}-' + ('prefetch' if enabled else 'sequential')
            root = args.out/name
            root.mkdir()
            path = root/'output.pdf'
            cache = root/'lines.json'
            refiner = ocr.LineRefiner(cache, set(range(1, len(chosen)+1)), automatic=True, source_sha256=sha)
            try:
                start = time.perf_counter()
                report = ocr.overlay(source, chosen, path, automatic=True, line_refiner=refiner, prefetch=enabled)
                seconds = time.perf_counter()-start
                decisions = copy.deepcopy(refiner.cache['decisions'])
            finally:
                refiner.close()
            if reference_report is None:
                reference_report, reference_decisions, reference_path = comparable(report), decisions, path
            assert comparable(report) == reference_report, 'Changed quality/layout report'
            assert decisions == reference_decisions, 'Changed text/order/crop keys/alignment decisions'
            with fitz.open(reference_path) as before, fitz.open(path) as after:
                for index in range(len(before)):
                    assert before[index].get_text('rawdict') == after[index].get_text('rawdict'), 'Changed glyphs/coordinates'
                    assert before[index].get_pixmap().samples == after[index].get_pixmap().samples, 'Changed pixels'
            entry = {'round': round_index, 'prefetch': enabled, 'seconds': seconds,
                     'stats': report.get('prefetch_stats'), 'exact_output': True}
            # A fully warm cache must neither call OCR nor pre-render PNG crops.
            refiner = ocr.LineRefiner(cache, set(range(1, len(chosen)+1)), automatic=True, source_sha256=sha)
            try:
                with patch.object(ocr, 'recognize_lines', side_effect=AssertionError('warm cache called HTTP')):
                    start = time.perf_counter()
                    warm = ocr.overlay(source, chosen, root/'warm.pdf', automatic=True,
                                       line_refiner=refiner, prefetch=enabled)
                    entry['warm_seconds'] = time.perf_counter()-start
                    assert comparable(warm) == reference_report
                    assert refiner.cache['decisions'] == reference_decisions
            finally:
                refiner.close()
            runs.append(entry)
            result = {'pages': numbers, 'rounds': args.rounds, 'runs': runs,
                      'decisions': len(reference_decisions),
                      'lines': sum(len(d['rects']) for d in reference_decisions),
                      'median_seconds': {str(flag): statistics.median(r['seconds'] for r in runs if r['prefetch'] == flag)
                                         for flag in (False, True) if any(r['prefetch'] == flag for r in runs)}}
            atomic_json(args.out/'comparison.json', result)
            print(json.dumps(entry), flush=True)
    print(json.dumps(result['median_seconds']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('layout', type=Path)
    parser.add_argument('--pages', default='38,66,147,166,188')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--out', required=True, type=Path)
    run(parser.parse_args())
