"""Offline A/B of text processing using recorded decisions and repeated text.

The explicit baseline module is a pre-change ocr_text.py snapshot. Databases
are opened read-only; no OCR service, source PDF, or existing cache is modified.
"""
import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_text


def evaluate(module, rows):
    return [
        (module.line_boundaries_are_usable(row['original'], row['candidate']),
         module.align_to_recognized_lines(row['original'], row['candidate']),
         module.recognition_disagrees(row['original'], row['candidate']),
         module.line_refinement_is_safe(row['original'], row['candidate']))
        for row in rows
    ]


def measure(baseline, rows, repeats):
    samples = {'baseline': [], 'candidate': []}
    results = {}
    for iteration in range(repeats):
        order = [('baseline', baseline), ('candidate', ocr_text)]
        for name, module in order if iteration % 2 == 0 else reversed(order):
            start = time.perf_counter()
            current = evaluate(module, rows)
            samples[name].append(time.perf_counter() - start)
            if name in results and current != results[name]:
                raise AssertionError('Text processing was not repeatable')
            results[name] = current
    return {'rows': len(rows), 'seconds': samples,
            'median_seconds': {key: statistics.median(value) for key, value in samples.items()},
            'changed_rows': [i for i, (a, b) in enumerate(zip(results['baseline'], results['candidate']))
                             if a != b]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-module', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    if args.out.exists():
        parser.error('--out must be a new file')
    spec = importlib.util.spec_from_file_location('text_benchmark_baseline', args.baseline_module)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    with closing(sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = [json.loads(value) for (value,) in db.execute('SELECT value FROM decisions ORDER BY id')]
    original = 'Repeated text 123. ' * 300
    report = {
        'baseline_sha256': hashlib.sha256(args.baseline_module.read_bytes()).hexdigest(),
        'candidate_sha256': hashlib.sha256(Path(ocr_text.__file__).read_bytes()).hexdigest(),
        'database': str(args.database.resolve()),
        'recorded': measure(baseline, rows, args.repeats),
        'synthetic_repeated': measure(baseline, [{'original': original,
            'candidate': [original[:950], original[950:1900], original[1900:]]}], args.repeats),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
