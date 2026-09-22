"""Bounded source-only repair geometry preparation with sticky serial recovery.

Independent processes own their PDF readers. Only the caller uses source-page
objects, OCR responses, mutable entries and checkpoint callbacks. No cache writes.
"""
import atexit
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import signal
import threading
import time

import pymupdf as fitz
try:
    import psutil
except ImportError:
    psutil = None

from ocr_render import PageRaster, ZOOM, detect_lines
from ocr_schema import _find_size, _norm_bbox

MIB = 1024**2
SYSTEM_RESERVE = 2048*MIB
WORKER_BASE = 512*MIB
# Four workers were measured with the real GPU pipeline; higher client limits
# still apply to overlay preparation. This stage has a separate measured cap.
MAX_WORKERS = 4
_document = None
_init_error = None
_source = _source_stamp = None


def available_memory():
    return psutil.virtual_memory().available if psutil is not None else 0


def source_stamp(source):
    stat = Path(source).stat()
    return stat.st_size, stat.st_mtime_ns


def repair_rect(page, raw, size):
    """The original scaled/clipped repair rectangle, shared with the owner loop."""
    bbox = _norm_bbox(raw)
    if not bbox:
        return None
    sx, sy = page.rect.width/size[0], page.rect.height/size[1]
    rect = fitz.Rect(bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy) & page.rect
    return None if rect.is_empty or rect.width < 4 or rect.height < 4 else rect


def is_repair_entry(entry, resume):
    return (isinstance(entry, dict) and not (resume and 'repair_audit' in entry)
            and isinstance(entry.get('block_content'), str)
            and isinstance(entry.get('block_bbox'), (list, tuple)))


def eligible_entries(pruned, resume):
    if not isinstance(pruned, dict):
        return
    entries = pruned.get('parsing_res_list')
    if not isinstance(entries, list):
        return
    for entry in entries:
        if is_repair_entry(entry, resume):
            yield entry


def close_worker():
    global _document
    if _document is not None:
        _document.close()
    _document = None


def initialize_worker(source, stamp, aa):
    global _document, _init_error, _source, _source_stamp
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        _source, _source_stamp = source, stamp
        if source_stamp(source) != stamp:
            raise ValueError('Repair source changed before worker startup')
        fitz.TOOLS.set_aa_level(aa['graphics'])
        if fitz.TOOLS.show_aa_level() != aa:
            raise ValueError('Repair worker cannot reproduce antialias settings')
        _document = fitz.open(source)
        _init_error = None
        atexit.register(close_worker)
    except Exception as error:
        # Never raise from Pool initializer: it would repeatedly respawn workers.
        _init_error = str(error)


def prepare_page(job):
    if _init_error:
        raise RuntimeError(_init_error)
    if source_stamp(_source) != _source_stamp:
        raise ValueError('Repair source changed during preparation')
    if available_memory() < SYSTEM_RESERVE:
        raise MemoryError('Repair worker system reserve')
    page = _document[job['page']]
    try:
        raster = PageRaster(page)
        boxes = {}
        for rectangle in job['rectangles']:
            if available_memory() < SYSTEM_RESERVE:
                raise MemoryError('Repair worker memory pressure')
            boxes[tuple(rectangle)] = [list(box) for box in detect_lines(page, fitz.Rect(rectangle), raster=raster)]
        return {'page': job['page'], 'key': job['key'], 'boxes': boxes}
    finally:
        fitz.TOOLS.store_shrink(100)


def validate_result(result, job):
    if not isinstance(result, dict) or result.get('page') != job['page'] or result.get('key') != job['key']:
        raise ValueError('Repair worker result identity mismatch')
    boxes = result.get('boxes')
    if not isinstance(boxes, dict) or set(boxes) != {tuple(r) for r in job['rectangles']}:
        raise ValueError('Repair worker result rectangle mismatch')
    for values in boxes.values():
        if not isinstance(values, list) or not all(
                isinstance(box, list) and len(box) == 4
                and all(type(v) in (int, float) and math.isfinite(v) for v in box)
                and box[0] < box[2] and box[1] < box[3] for box in values):
            raise ValueError('Invalid repair worker geometry')
    return boxes


class RepairCPUPreparer:
    def __init__(self, source, pages, *, workers=0, memory_mb=16384, resume=False,
                 timeout=120, stats=None, memory_probe=available_memory,
                 pool_factory=None, worker_fn=None):
        self.owner = threading.get_ident()
        self.source, self.pages, self.resume = str(source), pages, resume
        self.requested = workers
        self.limit = max(0, min(workers, MAX_WORKERS, os.cpu_count() or 1))
        self.budget, self.timeout, self.memory_probe = memory_mb*MIB, timeout, memory_probe
        self.pool_factory, self.worker_fn = pool_factory, worker_fn or prepare_page
        self.disabled = self.limit == 0 or psutil is None or memory_mb < 256 or timeout <= 0
        self.pool = None
        self.capacity = 0
        self.observed_workers = {}
        self.jobs, self.pending = {}, {}
        self.queue, self.next_job = [], 0
        self.current_page, self.current = None, {}
        self.closed = False
        self.stats = stats if stats is not None else {}
        self.stats.update(requested_workers=workers, stage_limit=self.limit, pool_workers=0, pool_starts=0,
                          prepared_pages=0, serial_detections=0, empty_retries=0, peak_pending=0,
                          peak_worker_rss_mib=0, wait_seconds=0.0, fallback=None)
        self.stats.update(peak_running_pages=0, peak_reserved_mib=0)

    def _owner(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError('Repair preparation belongs to its caller thread')

    def __enter__(self):
        self._owner()
        return self  # Completed resume/opt-out must not even open a planning PDF.

    def close(self):
        self._owner()
        if self.pool is not None:
            pool, self.pool = self.pool, None
            pool.terminate()
            pool.join()
        self.pending.clear()
        self.observed_workers.clear()
        self.jobs.clear()
        self.queue.clear()
        self.current.clear()
        self.closed = True

    def __exit__(self, *args):
        self.close()

    def recover(self, error):
        # Only worker/preparation failures reach here, never an OCR checkpoint
        # callback. Already-applied OCR results remain with the caller.
        self.disabled = True
        self.stats['fallback'] = str(error)
        self.close()
        print(f'[repair-cpu] {error}; continuing serially', flush=True)

    def start(self, first):
        stamp = source_stamp(self.source)
        with fitz.open(self.source) as doc:
            for pno in range(first, min(len(doc), len(self.pages))):
                pruned = self.pages[pno]
                entries = list(eligible_entries(pruned, self.resume))
                if not entries:
                    continue
                size = _find_size(pruned)
                if not size or size[0] <= 0 or size[1] <= 0:
                    continue
                page = doc[pno]
                rectangles, largest = [], 0
                for entry in entries:
                    rect = repair_rect(page, entry['block_bbox'], size)
                    if rect is not None:
                        rectangles.append(list(rect))
                        largest = max(largest, (math.ceil(rect.width*ZOOM)+2)*(math.ceil(rect.height*ZOOM)+2))
                if not rectangles:
                    continue
                images = {image[0]: image for image in page.get_images(full=True)}
                decoded = sum(image[2]*image[3]*4 for image in images.values())
                reservation = WORKER_BASE + 2*decoded + 12*largest
                key = hashlib.sha256(json.dumps([stamp, pno, rectangles]).encode()).hexdigest()
                self.jobs[pno] = {'page': pno, 'rectangles': rectangles, 'reservation': reservation, 'key': key}
        if len(self.jobs) < 2:
            self.disabled = True
            self.stats['admission'] = 'fewer than two eligible pages'
            return
        # Reserve every interpreter, including idle ones, and enough working
        # memory for the largest page. fill() admits each concurrent raster by
        # its own size: a large last page must not serialize all smaller pages.
        reservation = max(job['reservation'] for job in self.jobs.values())
        self.capacity = min(self.budget, max(0, (self.memory_probe()-SYSTEM_RESERVE)//2))
        workers = min(self.limit, len(self.jobs),
                      (self.capacity-(reservation-WORKER_BASE))//WORKER_BASE)
        if workers < 1:
            self.disabled = True
            self.stats['admission'] = 'insufficient memory'
            return
        self.stats.update(pool_workers=workers, per_worker_reservation_mib=reservation/MIB,
                          admission_capacity_mib=self.capacity/MIB,
                          admission='page-weighted startup')
        factory = self.pool_factory or mp.get_context('spawn').Pool
        self.pool = factory(workers, initializer=initialize_worker,
            initargs=(self.source, stamp, fitz.TOOLS.show_aa_level()), maxtasksperchild=16)
        self.observed_workers = {worker.pid: worker for worker in self.pool._pool}
        self.stats['pool_starts'] += 1
        self.queue = list(self.jobs)
        self.fill()
        print(f'[repair-cpu] {workers}/{self.requested} workers (stage cap {MAX_WORKERS}), '
              f'budget {self.budget//MIB} MiB, admitted {self.capacity//MIB} MiB, '
              f'largest page {reservation/MIB:.0f} MiB', flush=True)

    def fill(self):
        # Ready results contain only geometry. Their raster reservation can be
        # reused before the owner consumes them, while result order stays fixed.
        workers = self.stats['pool_workers']
        running = [pno for pno, future in self.pending.items() if not future.ready()]
        reserved = workers*WORKER_BASE + sum(
            self.jobs[pno]['reservation']-WORKER_BASE for pno in running)
        while (len(self.pending) < 2*workers and len(running) < workers
               and self.next_job < len(self.queue)):
            pno = self.queue[self.next_job]
            extra = self.jobs[pno]['reservation']-WORKER_BASE
            if reserved+extra > self.capacity:
                break
            self.pending[pno] = self.pool.apply_async(self.worker_fn, (self.jobs[pno],))
            running.append(pno)
            reserved += extra
            self.next_job += 1
        self.stats['peak_pending'] = max(self.stats['peak_pending'], len(self.pending))
        self.stats['peak_running_pages'] = max(self.stats['peak_running_pages'], len(running))
        self.stats['peak_reserved_mib'] = max(self.stats['peak_reserved_mib'], reserved/MIB)

    def check_memory(self):
        rss = 0
        for worker in self.pool._pool:
            self.observed_workers[worker.pid] = worker
        for pid, worker in list(self.observed_workers.items()):
            if worker.exitcode not in (None, 0):
                raise RuntimeError(f'Repair worker exited with code {worker.exitcode}')
            if worker.exitcode == 0:
                self.observed_workers.pop(pid)
        for worker in self.pool._pool:
            try:
                rss += psutil.Process(worker.pid).memory_info().rss
            except psutil.Error:
                pass
        self.stats['peak_worker_rss_mib'] = max(self.stats['peak_worker_rss_mib'], rss/MIB)
        if rss > self.budget or self.memory_probe() < SYSTEM_RESERVE:
            raise MemoryError('Repair preparation memory pressure')

    def detect(self, page, rect, *, raster=None, serial=detect_lines, on_wait=None):
        self._owner()
        pno = page.number
        if not self.disabled and not self.closed:
            try:
                if self.pool is None:
                    self.start(pno)
                if self.pool is not None:
                    self.fill()
                if self.pool is not None and pno != self.current_page:
                    job = self.jobs[pno]
                else:
                    job = None
            except Exception as error:
                self.recover(error)
                job = None
            if not self.disabled and job is not None:
                started = time.perf_counter()
                try:
                    while True:
                        # Outside the CPU-error catch: failed checkpoint writes
                        # or user interruption must abort, not look like fallback.
                        if on_wait is not None:
                            on_wait()
                        try:
                            self.check_memory()
                            self.fill()
                            future = self.pending.get(pno)
                            if future is None:
                                # Earlier rasters may occupy the budget even if
                                # an interpreter is idle. Do not bypass admission.
                                time.sleep(.05)
                                raise mp.TimeoutError()
                            result = future.get(timeout=.05)
                            boxes = validate_result(result, job)
                            self.current_page, self.current = pno, boxes
                            self.pending.pop(pno)
                            self.jobs.pop(pno)
                            self.stats['prepared_pages'] += 1
                            self.fill()
                        except mp.TimeoutError:
                            if time.perf_counter()-started <= self.timeout:
                                continue
                            self.recover(TimeoutError('Repair preparation page timeout'))
                        except Exception as error:
                            self.recover(error)
                        break
                finally:
                    self.stats['wait_seconds'] += time.perf_counter()-started
            if not self.disabled:
                try:
                    values = self.current[tuple(rect)]
                    if values:
                        return [fitz.Rect(box) for box in values]
                    # A speculative empty result might be a transient rendering
                    # failure. Retry only at the original owner position.
                    self.stats['empty_retries'] += 1
                except Exception as error:
                    self.recover(error)
        self.stats['serial_detections'] += 1
        return serial(page, rect, raster=raster)
