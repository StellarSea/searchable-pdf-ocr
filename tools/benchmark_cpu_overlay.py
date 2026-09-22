"""Experimental CPU page pool connected to the real overlay and GPU HTTP path.

No CLI default changes, source/cache mutations, or service restarts. Each timed
run uses fresh diagnostic caches. PDF verification is timed separately.
"""
import argparse
import copy
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

import psutil
import pymupdf as fitz
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_to_searchable_pdf as ocr
from ocr_storage import atomic_json
from ocr_workflow import verify
from benchmark_cpu_workers import initialize_worker, render_page, MemoryMonitor
from benchmark_prefetch import comparable


class PreparedPages:
    """Bounded lookahead; only the caller owns SQLite writes and PDF insertion.

    At most workers futures plus the current page's PNGs are retained. A failed
    page returns None so the original layout/render path retries without loss.
    This is an experiment, not an automatic warm-cache scheduling policy.
    """
    def __init__(self, source, layouts, refiner, workers, font):
        if not layouts or workers < 1:
            raise ValueError('Prepared pages require positive workers and nonempty layouts')
        self.source, self.layouts, self.refiner = source, layouts, refiner
        # Otherwise a short sample can wait forever at an oversized barrier.
        self.workers, self.font = min(workers, len(layouts)), font
        self.pool = None
        self.pending = {}
        self.next_page = 0
        self.stats = {'pages': 0, 'fallbacks': [], 'wait_seconds': 0.0,
                      'png_bytes': 0, 'peak_pending_pages': 0}

    def fill(self):
        while len(self.pending) < self.workers and self.next_page < len(self.layouts):
            pno = self.next_page
            self.pending[pno] = self.pool.submit(render_page, (pno, self.layouts[pno]))
            self.next_page += 1
        self.stats['peak_pending_pages'] = max(self.stats['peak_pending_pages'], len(self.pending))

    def __enter__(self):
        ctx = mp.get_context('spawn')
        self.pool = ProcessPoolExecutor(max_workers=self.workers, mp_context=ctx,
                                        initializer=initialize_worker,
                                        initargs=(str(self.source), self.refiner.source_sha256,
                                                  str(self.font), ctx.Barrier(self.workers)))
        try:
            self.fill()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __call__(self, pno):
        started = time.perf_counter()
        try:
            # Existing-text pages can be skipped by overlay without a callback.
            for old in [number for number in self.pending if number < pno]:
                self.pending.pop(old).cancel()
            self.next_page = max(self.next_page, pno)
            self.fill()
            result = self.pending.pop(pno).result()
            if result['page'] != pno:
                raise ValueError('Prepared page identity mismatch')
            blocks = {}
            for block in result['blocks']:
                # Empty speculative detection may represent a transient failure.
                if not block['rects']:
                    continue
                blocks[block['index']] = ([fitz.Rect(rect) for rect in block['rects']],
                                           block['chunks'],
                                           {tuple(crop['rect']): crop['png'] for crop in block['crops']})
            for key, boxes in result.get('detections', {}).items():
                self.refiner.store.save_detection(key, boxes)
            self.stats['pages'] += 1
            self.stats['png_bytes'] += result['png_bytes']
            return blocks
        except Exception as error:
            self.stats['fallbacks'].append({'page': pno+1, 'error': str(error)})
            return None
        finally:
            self.stats['wait_seconds'] += time.perf_counter()-started

    def __exit__(self, *args):
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
        self.pending.clear()


def assert_pdf_equal(reference, candidate):
    with fitz.open(reference) as before, fitz.open(candidate) as after:
        assert len(before) == len(after), 'Page count changed'
        for a, b in zip(before, after):
            assert a.get_text('rawdict') == b.get_text('rawdict'), 'Glyphs or coordinates changed'
            ap, bp = a.get_pixmap(), b.get_pixmap()
            assert ap.irect == bp.irect and ap.samples == bp.samples, 'Rendered pixels changed'


def run(args):
    if args.out.exists():
        raise ValueError('Use a new diagnostic output directory')
    health = requests.get('http://127.0.0.1:8081/health', timeout=10)
    health.raise_for_status()
    if psutil.virtual_memory().available < 8*1024**3:
        raise RuntimeError('This eight-page experiment requires 8 GiB available memory')
    numbers = [int(n) for n in args.pages.split(',')]
    layouts = json.loads(args.layout.read_text(encoding='utf-8'))
    chosen = [layouts[n-1] if isinstance(layouts, list) else layouts['pages'][str(n-1)]['result'] for n in numbers]
    args.out.mkdir(parents=True)
    source = args.out/'source.pdf'
    with fitz.open(args.source) as doc:
        doc.select([n-1 for n in numbers])
        doc.save(source)
    with source.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    report = {'pages': numbers, 'rounds': args.rounds, 'service': health.json(), 'runs': [],
              'scope': 'real GPU OCR + overlay/save + process startup/shutdown; verification timed separately',
              'production': args.production,
              'warm_policy': 'automatic cache-first' if args.production else 'explicitly bypass experimental pool'}
    reference_report = reference_decisions = reference_path = None
    original_recognize = ocr.recognize_lines
    for round_index in range(args.rounds):
        order = list(args.workers)
        offset = round_index % len(order)
        order = order[offset:]+order[:offset]
        for workers in order:
            root = args.out/f'round-{round_index}-workers-{workers}'
            root.mkdir()
            path, cache = root/'output.pdf', root/'lines.json'
            refiner = ocr.LineRefiner(cache, set(range(1, len(chosen)+1)), automatic=True, source_sha256=sha)
            calls = []
            def recognize(images, **kwargs):
                started = time.perf_counter()
                values = original_recognize(images, **kwargs)
                calls.append({'seconds': time.perf_counter()-started, 'lines': len(images)})
                return values
            try:
                with MemoryMonitor() as memory, patch.object(ocr, 'recognize_lines', side_effect=recognize):
                    started = time.perf_counter()
                    manager = (PreparedPages(source, chosen, refiner, workers, args.font)
                               if workers and not args.production else nullcontext(None))
                    with manager as prepared:
                        quality = ocr.overlay(source, chosen, path, automatic=True, line_refiner=refiner,
                                              prepared_pages=prepared,
                                              cpu_workers=workers if args.production else 0)
                    elapsed = time.perf_counter()-started
                decisions = copy.deepcopy(refiner.cache['decisions'])
                started = time.perf_counter()
                verified = verify(source, path, copy.deepcopy(quality))
                verify_seconds = time.perf_counter()-started
                assert not verified['validation_failed'], 'Final PDF failed production validation'
                if reference_report is None:
                    reference_report = comparable(verified)
                    reference_decisions, reference_path = decisions, path
                assert comparable(verified) == reference_report, 'Quality report changed'
                assert decisions == reference_decisions, 'OCR candidates/keys/order/decisions changed'
                assert_pdf_equal(reference_path, path)
                atomic_json(root/'quality.json', verified)
                entry = {'round': round_index, 'workers': workers, 'overlay_seconds': elapsed,
                         'verify_seconds': verify_seconds, 'total_seconds': elapsed+verify_seconds,
                         'http_seconds_sum': sum(call['seconds'] for call in calls),
                         'http_calls': len(calls), 'http_lines': sum(call['lines'] for call in calls),
                         'peak_rss_mib': memory.peak_rss/1024**2,
                         'minimum_available_mib': memory.minimum_available/1024**2,
                         'pool': quality.get('cpu_prepare_stats', prepared.stats if prepared else None), 'exact': True}
                if prepared:
                    assert not prepared.stats['fallbacks'], 'Unexpected CPU fallback: inspect this run'
            finally:
                refiner.close()
            # Check persisted worker detections/crop indices via the unchanged
            # cache-first path. Starting a cold page pool here would waste work.
            refiner = ocr.LineRefiner(cache, set(range(1, len(chosen)+1)), automatic=True, source_sha256=sha)
            try:
                with patch.object(ocr, 'recognize_lines', side_effect=AssertionError('Warm cache called HTTP')):
                    started = time.perf_counter()
                    warm = ocr.overlay(source, chosen, root/'warm.pdf', automatic=True, line_refiner=refiner,
                                       cpu_workers=workers if args.production else 0)
                    entry['warm_seconds'] = time.perf_counter()-started
                entry['warm_pool'] = warm.get('cpu_prepare_stats')
                if args.production and workers:
                    assert entry['warm_pool']['pool_starts'] == 0, 'Warm cache started CPU workers'
                    assert not entry['warm_pool']['fallbacks'], 'Warm cache triggered sticky recovery'
                assert refiner.cache['decisions'] == reference_decisions
                assert comparable(verify(source, root/'warm.pdf', warm)) == reference_report
                assert_pdf_equal(reference_path, root/'warm.pdf')
            finally:
                refiner.close()
            report['runs'].append(entry)
            report['medians'] = {str(n): {key: statistics.median(r[key] for r in report['runs'] if r['workers'] == n)
                                          for key in ('overlay_seconds', 'verify_seconds', 'total_seconds', 'warm_seconds')}
                                 for n in args.workers if any(r['workers'] == n for r in report['runs'])}
            report['decisions'] = len(reference_decisions)
            atomic_json(args.out/'comparison.json', report)
            print(json.dumps(entry), flush=True)
    # Inspection previews are diagnostic, never published over the user's PDF.
    with fitz.open(reference_path) as doc:
        for index, number in enumerate(numbers):
            page = doc[index]
            page.get_pixmap(matrix=fitz.Matrix(1500/page.rect.height, 1500/page.rect.height)).save(
                args.out/f'page-{number}.png')
    print(json.dumps(report['medians']), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('layout', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--pages', default='6,7,29,38,66,147,166,188')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--production', action='store_true', help='Test production CPU/cache/budget scheduling')
    parser.add_argument('--workers', type=int, nargs='+', choices=range(9), default=[0, 4, 8])
    parser.add_argument('--font', type=Path, default=Path('C:/Windows/Fonts/malgun.ttf'))
    options = parser.parse_args()
    if options.rounds < 1:
        parser.error('rounds must be positive')
    run(options)
