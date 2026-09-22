"""Experimental bounded process preparation for repair detection; offline by default.

No production defaults are changed. Workers independently read source PDF pages
and return geometry, never OCR responses, PDF writes or SQLite access. The owner
replays the original repair loop with identical recorded OCR responses/checkpoints.
Startup/planning/cleanup are included in wall time. This is not whole-OCR speed.
"""
import argparse
import atexit
import cProfile
import math
import multiprocessing as mp
from pathlib import Path
import signal
import statistics
import sys
import time
from unittest.mock import patch

import psutil
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
from ocr_render import PageRaster, detect_lines, ZOOM
from ocr_schema import _find_size, _norm_bbox
from ocr_storage import atomic_json
import benchmark_repair_render as replay

MIB = 1024**2
RESERVE = 2048*MIB
_document = None
_init_error = None


def close_worker():
    global _document
    if _document is not None:
        _document.close()
    _document = None


def initialize_worker(source):
    global _document, _init_error
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        _document = fitz.open(source)
        _init_error = None
        atexit.register(close_worker)
    except Exception as error:
        _init_error = str(error)


def detect_page(job):
    if _init_error:
        raise RuntimeError(_init_error)
    pno, rectangles, reservation = job
    if psutil.virtual_memory().available < RESERVE:
        raise MemoryError('Repair experiment system reserve')
    page = _document[pno]
    raster = PageRaster(page)
    try:
        found = {tuple(rect): [list(box) for box in detect_lines(page, fitz.Rect(rect), raster=raster)]
                 for rect in rectangles}
        return pno, found
    finally:
        fitz.TOOLS.store_shrink(100)


def plan(source, pages):
    jobs = []
    with fitz.open(source) as doc:
        for pno, pruned in enumerate(pages):
            if pno >= len(doc) or not isinstance(pruned, dict):
                continue
            entries, size = pruned.get('parsing_res_list'), _find_size(pruned)
            if not isinstance(entries, list) or not size or min(size) <= 0:
                continue
            page = doc[pno]
            sx, sy = page.rect.width/size[0], page.rect.height/size[1]
            rectangles, largest = [], 0
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get('block_content'), str):
                    continue
                raw = entry.get('block_bbox')
                if not isinstance(raw, (list, tuple)):
                    continue
                bbox = _norm_bbox(raw)
                if not bbox:
                    continue
                rect = fitz.Rect(bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy) & page.rect
                if rect.is_empty or rect.width < 4 or rect.height < 4:
                    continue
                rectangles.append(list(rect))
                largest = max(largest, (math.ceil(rect.width*ZOOM)+2)*(math.ceil(rect.height*ZOOM)+2))
            if rectangles:
                images = {image[0]: image for image in page.get_images(full=True)}
                decoded = sum(image[2]*image[3]*4 for image in images.values())
                # Conservative native decode/array/interpreter headroom. This
                # admission estimate is not an OS-enforced allocation ceiling.
                reservation = 512*MIB + 2*decoded + 12*largest
                jobs.append((pno, rectangles, reservation))
    return jobs


class PreparedDetections:
    def __init__(self, source, pages, workers, memory_mb=8192, timeout=120):
        if not 1 <= workers <= 8 or memory_mb < 256 or timeout <= 0:
            raise ValueError('Invalid experimental pool limits')
        self.source, self.pages = str(source), pages
        self.requested, self.budget, self.timeout = workers, memory_mb*MIB, timeout
        self.pool = None
        self.pending = {}
        self.current_page, self.current = None, {}
        self.next_job = 0
        self.stats = {'requested_workers': workers, 'workers': 0, 'prepared_pages': 0,
                      'peak_pending': 0, 'peak_worker_rss_mib': 0, 'wait_seconds': 0.0}

    def __enter__(self):
        try:
            self.jobs = plan(self.source, self.pages)
            reservation = max((job[2] for job in self.jobs), default=self.budget+1)
            capacity = min(self.budget, max(0, (psutil.virtual_memory().available-RESERVE)//2))
            self.workers = min(self.requested, psutil.cpu_count() or 1, len(self.jobs), capacity//reservation)
            if self.workers < 1:
                raise MemoryError('No admissible repair experiment worker')
            self.stats.update(workers=self.workers, per_worker_reservation_mib=reservation/MIB)
            self.pool = mp.get_context('spawn').Pool(self.workers, initializer=initialize_worker,
                                                  initargs=(self.source,), maxtasksperchild=16)
            self.fill()
            return self
        except BaseException:
            self.close()
            raise

    def fill(self):
        while len(self.pending) < 2*self.workers and self.next_job < len(self.jobs):
            job = self.jobs[self.next_job]
            self.pending[job[0]] = self.pool.apply_async(detect_page, (job,))
            self.next_job += 1
        self.stats['peak_pending'] = max(self.stats['peak_pending'], len(self.pending))

    def sample_memory(self):
        rss = 0
        for worker in self.pool._pool:
            try:
                rss += psutil.Process(worker.pid).memory_info().rss
            except psutil.Error:
                pass
        self.stats['peak_worker_rss_mib'] = max(self.stats['peak_worker_rss_mib'], rss/MIB)
        if rss > self.budget or psutil.virtual_memory().available < RESERVE:
            raise MemoryError('Repair experiment runtime memory limit')

    def detect(self, page, rect, raster=None):
        pno = page.number
        if pno != self.current_page:
            started = time.perf_counter()
            future = self.pending.pop(pno)
            while True:
                self.sample_memory()
                try:
                    result_page, found = future.get(timeout=.1)
                    break
                except mp.TimeoutError:
                    if time.perf_counter()-started > self.timeout:
                        raise TimeoutError('Repair experiment page timeout')
            self.stats['wait_seconds'] += time.perf_counter()-started
            if result_page != pno:
                raise ValueError('Repair experiment page identity mismatch')
            self.current_page, self.current = pno, found
            self.stats['prepared_pages'] += 1
            self.fill()
        # Missing keys are hard failures in this proof, never silently masked
        # by a serial fallback that might make a faulty candidate appear exact.
        return [fitz.Rect(box) for box in self.current[tuple(rect)]]

    def close(self):
        if self.pool is not None:
            pool, self.pool = self.pool, None
            pool.terminate()
            pool.join()
        self.pending.clear()

    def __exit__(self, *args):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--workers', nargs='+', type=int, default=[0, 2, 4, 8])
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--profile-serial', action='store_true')
    parser.add_argument('--live', action='store_true', help='Explicitly send repair crops to the current OCR service')
    parser.add_argument('--prefetch', action='store_true', help='Keep the same HTTP overlap setting for all variants')
    parser.add_argument('--production', action='store_true', help='Measure the integrated lazy pool and serial recovery path')
    args = parser.parse_args()
    if args.repeats < 1 or any(n < 0 or n > 8 for n in args.workers):
        parser.error('Positive repeats and workers 0..8 required')
    if args.live and args.profile_serial:
        parser.error('Function profiling is reserved for the offline serial baseline')
    pages, candidates = replay.load_replay(args.source, args.baseline)
    args.out.mkdir(parents=True, exist_ok=False)
    records = []
    if args.profile_serial:
        profiler = cProfile.Profile()
        record = profiler.runcall(replay.run, args.source, pages, candidates, True)
        profiler.dump_stats(args.out/'serial.pstats')
        atomic_json(args.out/'profiled-serial.json', record)
    for repeat in range(args.repeats):
        for workers in args.workers if repeat % 2 == 0 else reversed(args.workers):
            print(f'[repair-pool] start workers={workers}', flush=True)
            started = time.perf_counter()
            if args.production:
                record = replay.run(args.source, pages, candidates, True,
                                    prefetch=args.prefetch, live=args.live, cpu_workers=workers)
            elif workers:
                with PreparedDetections(args.source, pages, workers) as prepared:
                    with patch.object(replay.ocr, 'detect_lines', side_effect=prepared.detect):
                        record = replay.run(args.source, pages, candidates, True,
                                            prefetch=args.prefetch, live=args.live)
                record['cpu'] = prepared.stats
            else:
                record = replay.run(args.source, pages, candidates, True,
                                    prefetch=args.prefetch, live=args.live)
            record.update(workers=workers, total_seconds=time.perf_counter()-started)
            record['exact_decisions'] = not records or record['exact'] == records[0]['exact']
            atomic_json(args.out/f'run-{len(records)+1}.json', record)
            if records and any(record['exact'][key] != records[0]['exact'][key] for key in ('boxes', 'images')):
                raise AssertionError('Repair request pixels or geometry changed')
            if not args.live and not record['exact_decisions']:
                raise AssertionError('Repair pixels, boxes, text or checkpoints changed')
            records.append(record)
            print(f"[repair-pool] workers={workers}: {record['total_seconds']:.3f}s, exact decisions={record['exact_decisions']}", flush=True)
    result = {'records': records, 'exact': all(r['exact_decisions'] for r in records), 'exact_request_inputs': True,
              'median_seconds': {
        str(n): statistics.median(r['total_seconds'] for r in records if r['workers'] == n) for n in args.workers},
              'live': args.live, 'prefetch': args.prefetch, 'production': args.production,
              'scope': ('Repair only; integrated production pool' if args.production else
                        'Repair only; standalone experimental pool') + '; live model variation not excluded'}
    atomic_json(args.out/'comparison.json', result)
    print({k: v for k, v in result.items() if k != 'records'}, flush=True)
    if not result['exact']:
        raise SystemExit('Live OCR decisions differed; inspect saved responses, not an exact accuracy pass')


if __name__ == '__main__':
    main()
