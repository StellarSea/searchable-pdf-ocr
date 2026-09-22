"""Exact reference/candidate comparison for long horizontal ink runs.

The reference is the pre-optimization NumPy opening, retained as an oracle.
This tool never calls OCR, writes PDFs, or changes a server setting.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
from ocr_render import _long_runs


def reference(mask, length):
    h, n = mask.shape
    if length < 2 or n < length:
        return np.zeros_like(mask)
    c = np.pad(np.cumsum(mask, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
    ero = (c[:, length:] - c[:, :-length]) == length
    m = ero.shape[1]
    ce = np.pad(np.cumsum(ero, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
    j = np.arange(n)
    hi = np.minimum(j + 1, m)
    lo = np.maximum(j - length + 1, 0)
    return (ce[:, hi] - ce[:, lo]) > 0


def candidate(mask, length):
    return _long_runs(mask, length)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError('Choose a new benchmark output')
    rng = np.random.default_rng(20260914)
    sparse = rng.random((1800, 2400)) < 0.07
    sparse[100:104, 20:2300] = True
    cases = [('text-rules', sparse, 60), ('text-rules-transposed', sparse.T, 60),
             ('solid', np.ones((1800, 2400), dtype=bool), 60),
             ('alternating-pairs', np.tile([True, True, False, False], (1800, 600)), 2)]
    records = []
    for name, mask, length in cases:
        expected = reference(mask, length)
        for implementation in (reference, candidate, candidate, reference):
            started = time.perf_counter()
            actual = implementation(mask, length)
            seconds = time.perf_counter() - started
            if not np.array_equal(actual, expected):
                raise AssertionError(f'Mask mismatch: {name}')
            del actual
            # Measure allocation separately from timing. NumPy registers its
            # data buffers with tracemalloc; this is not a process RSS measure.
            tracemalloc.start()
            actual = implementation(mask, length)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            del actual
            record = dict(case=name, implementation=implementation.__name__, seconds=seconds,
                          traced_peak_bytes=peak, exact=True)
            records.append(record)
            print(json.dumps(record), flush=True)
        del expected
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({'records': records, 'medians': {
        name: {impl: statistics.median(r['seconds'] for r in records
                                      if r['case'] == name and r['implementation'] == impl)
               for impl in ('reference', 'candidate')} for name, _, _ in cases}}, indent=2))


if __name__ == '__main__':
    main()
