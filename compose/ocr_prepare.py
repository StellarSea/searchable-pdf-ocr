"""Cache-first, memory-budgeted CPU preparation with an unchanged serial fallback.

Only the owner thread plans pages and accesses SQLite. Spawned processes open
their own source PDF/font and return plain geometry, strings and PNG bytes.
No OCR API, PDF writes, review decisions, or source-cache publication in workers.
"""
import atexit
import hashlib
import json
import math
import multiprocessing as mp
import os
import signal
import sys
import time

import pymupdf as fitz
try:
    import psutil
except ImportError:
    psutil = None  # Old installations remain functional on the serial path.

from ocr_layout import block_layout, page_blocks, needs_crops, crop_rect, line_settings
from ocr_render import PageRaster, ZOOM

MIB = 1024**2
PNG_BUDGET = 64*MIB
SYSTEM_RESERVE = 2048*MIB
_document = _font = None
_source_sha = _init_error = None


def available_memory():
    return psutil.virtual_memory().available if psutil is not None else 0


def retained_size(value):
    """Size plain worker results without copying their potentially large PNGs."""
    seen = set()
    def size(item):
        if id(item) in seen:
            return 0
        seen.add(id(item))
        total = sys.getsizeof(item)
        if isinstance(item, dict):
            total += sum(size(key)+size(child) for key, child in item.items())
        elif isinstance(item, (tuple, list)):
            total += sum(size(child) for child in item)
        return total
    # Include conversion/IPC headroom; actual RSS remains independently checked.
    return 2*size(value) + MIB


def close_worker():
    global _document, _font
    if _document is not None:
        _document.close()
    _document = _font = None


def initialize_worker(source, source_sha, font_spec):
    global _document, _font, _source_sha, _init_error
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        _document = fitz.open(source)
        _font = fitz.Font(**font_spec)
        _source_sha = source_sha
        if os.name == 'nt' and psutil is not None:
            try:
                psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            except psutil.Error:
                pass
        atexit.register(close_worker)
        _init_error = None
    except Exception as error:
        # Do not let Pool repeatedly respawn a failing initializer forever.
        _init_error = str(error)


class DetectionSnapshot:
    def __init__(self, values=None, owner=None):
        self.values = dict(values or {})
        self.owner = owner

    def detection(self, key):
        value = self.owner.detection(key) if self.owner is not None else self.values.get(key)
        if value is not None:
            self.values[key] = value
        return value

    def save_detection(self, key, boxes):
        self.values[key] = [list(box) for box in boxes]


class PixelsRequired(RuntimeError):
    """A direct table-geometry read must stay on the original owner path."""


class PlanningRaster(PageRaster):
    """Cache misses are observed, never rendered speculatively in the owner."""
    def __init__(self, *args):
        super().__init__(*args)
        self.missing = False

    def get_pixmap(self, **kwargs):
        self.missing = True
        raise PixelsRequired('Pixels required; schedule isolated preparation')


def layout_key(source_sha, pno, pruned):
    return hashlib.sha256(json.dumps([source_sha, pno, pruned], sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


def prepare_page(job):
    if _init_error:
        raise RuntimeError(_init_error)
    page = _document[job['page']]
    snapshot = DetectionSnapshot(job['detections'])
    raster = PageRaster(page, _source_sha, snapshot)
    blocks, retained = {}, 0
    try:
        for index, text, rect, reviewed in page_blocks(page, job['pruned']):
            boxes, chunks = block_layout(page, rect, text, reviewed, False, _font, raster)
            images = {}
            if job['ocr'] and needs_crops(text, boxes):
                settings = line_settings(text)
                for box in boxes:
                    crop = crop_rect(page, box)
                    if tuple(crop) in images or raster.crop_key(crop, settings) in job['skip_crops']:
                        continue
                    # Bound a single speculative allocation and retained IPC
                    # payload. Over-budget pages use the original renderer.
                    estimate = (math.ceil(crop.width*ZOOM)+2)*(math.ceil(crop.height*ZOOM)+2)*8
                    if estimate > job['reservation'] or available_memory() < SYSTEM_RESERVE:
                        raise MemoryError('CPU crop memory budget')
                    pix = raster.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=crop,
                                            colorspace=fitz.csRGB, alpha=False)
                    png = pix.tobytes('png')
                    retained += len(png)
                    if retained > PNG_BUDGET:
                        raise MemoryError('CPU page PNG budget')
                    images[tuple(crop)] = png
            # Speculative empty detection can mean a render failure. Retry it
            # normally later, just as the existing one-block lookahead does.
            if boxes:
                blocks[index] = ([list(box) for box in boxes], chunks, images)
        return {'page': job['page'], 'key': job['key'], 'blocks': blocks,
                'detections': snapshot.values, 'png_bytes': retained}
    finally:
        fitz.TOOLS.store_shrink(100)


class CPUPreparer:
    """Lazy Pool, bounded page window, cache bypass and sticky serial recovery.

    Estimates and sampled RSS are safeguards, not an OS-enforced memory limit.
    On pressure/error/timeout, terminate only this owned pool and leave all
    remaining pages to the old path. No hidden retry storm or reduced quality.
    """
    def __init__(self, source, layouts, store, source_sha, font_spec, ocr_pages,
                 workers=16, memory_mb=16384, timeout=120, memory_probe=available_memory):
        self.source, self.layouts, self.store = str(source), layouts, store
        self.source_sha, self.font_spec, self.ocr_pages = source_sha, font_spec, ocr_pages
        cores = (psutil.cpu_count(logical=True) if psutil is not None else None) or os.cpu_count() or 1
        self.workers = max(0, min(workers, cores, len(layouts)))
        self.window = 2*self.workers
        self.budget = memory_mb*MIB
        self.timeout, self.memory_probe = timeout, memory_probe
        self.doc = self.font = self.pool = None
        self.pool_workers = 0
        self.pending, self.ready = {}, {}
        self.completed, self.plans = {}, {}
        self.last_pump = 0.0
        self.on_ready = None  # Optional owner-thread observer; never a Pool callback.
        self.active_bytes = 0
        self.disabled = self.workers == 0 or not source_sha
        self.parent = psutil.Process() if psutil is not None else None
        self.parent_rss = self.parent.memory_info().rss if self.parent is not None else 0
        self.baseline_children = {p.pid for p in self.parent.children()} if self.parent is not None else set()
        self.stats = {'requested_workers': workers, 'workers': self.workers, 'pool_starts': 0,
                      'prepared_pages': 0, 'cached_pages': 0, 'serial_layout_pages': 0, 'memory_skips': 0,
                      'fallbacks': [], 'peak_pending_pages': 0, 'peak_reserved_mib': 0,
                      'peak_extra_rss_mib': 0, 'png_bytes': 0, 'wait_seconds': 0.0,
                      'peak_buffered_pages': 0, 'refill_jobs': 0,
                      'pool_workers': 0, 'memory_limited_pool': False}

    def __enter__(self):
        # No PDF open, worker spawn, or rendering until an eligible page asks.
        return self

    def stop_pool(self):
        if self.pool is not None:
            pool, self.pool = self.pool, None
            pool.terminate()
            pool.join()
        self.pending.clear()
        self.completed.clear()
        self.plans.clear()

    def __exit__(self, *args):
        try:
            self.stop_pool()
        finally:
            self.ready.clear()
            if self.doc is not None:
                self.doc.close()
            self.doc = self.font = None

    def fail(self, pno, error):
        self.stats['fallbacks'].append({'page': pno+1, 'error': str(error)})
        print(f'[cpu] p{pno+1}: serial fallback ({error})', flush=True)
        self.disabled = True
        self.stop_pool()
        self.ready.clear()

    def reserved(self):
        return (self.active_bytes + sum(item[1] for item in self.pending.values())
                + sum(item[1] for item in self.completed.values()))

    def collect(self):
        # Called only by the PDF/SQLite owner, never Pool callbacks/threads.
        for index, (result, reservation, key) in list(self.pending.items()):
            if result.ready():
                self.accept(index, key, result.get(timeout=0))

    def accept(self, index, key, value):
        if value['page'] != index or value['key'] != key:
            raise ValueError('CPU page/layout identity mismatch')
        self.completed[index] = (value, retained_size(value))
        self.pending.pop(index)

    def pump(self, pno, *, force=False, required=None):
        """Harvest and refill during CPU/HTTP waits, with bounded lookahead."""
        if self.disabled:
            return
        now = time.perf_counter()
        if not force and now-self.last_pump < .1:
            return
        self.last_pump = now
        try:
            if self.pressure():
                raise MemoryError('CPU pool memory pressure')
            self.collect()
            if self.on_ready is not None:
                self.on_ready((index, item[0]['blocks']) for index, item in sorted(self.completed.items()))
            self.fill(pno, required=required)
        except Exception as error:
            self.fail(pno, error)

    def pressure(self):
        available = self.memory_probe()
        if self.parent is not None:
            try:
                rss = max(0, self.parent.memory_info().rss-self.parent_rss)
                for process in self.parent.children():
                    if process.pid in self.baseline_children:
                        continue
                    try:
                        rss += process.memory_info().rss
                    except psutil.NoSuchProcess:
                        # Normal maxtasksperchild recycling is not pressure.
                        continue
                self.stats['peak_extra_rss_mib'] = max(self.stats['peak_extra_rss_mib'], rss/MIB)
                if rss > self.budget:
                    return True
            except psutil.Error:
                # Unknown memory state must not increase concurrency.
                return True
        return available < SYSTEM_RESERVE

    def plan(self, pno):
        if self.doc is None:
            self.doc = fitz.open(self.source)
            self.font = fitz.Font(**self.font_spec)
        page = self.doc[pno]
        if page.get_text().strip():
            return None, {}
        snapshot = DetectionSnapshot(owner=self.store)
        raster = PlanningRaster(page, self.source_sha, snapshot)
        cached, skip_crops = {}, set()
        requires_pixels, largest = False, 0
        for index, text, rect, reviewed in page_blocks(page, self.layouts[pno]):
            largest = max(largest, rect.get_area()*ZOOM*ZOOM)
            try:
                boxes, chunks = block_layout(page, rect, text, reviewed, False, self.font, raster)
            except PixelsRequired:
                # Primary lines may be cached while table column geometry still
                # requires pixels. Leave just this page on the cache-first owner
                # path; do not render in the planner or disable the whole pool.
                return None, None
            if boxes:
                cached[index] = (boxes, chunks, {})
            if pno+1 in self.ocr_pages and needs_crops(text, boxes):
                settings = line_settings(text)
                for box in boxes:
                    key = raster.crop_key(crop_rect(page, box), settings)
                    _, value = self.store.indexed_response(key)
                    if value is None:
                        requires_pixels = True
                    else:
                        skip_crops.add(key)
        if not raster.missing and not requires_pixels:
            return None, cached
        # Account for source decoding, NumPy working arrays, PNG+IPC copies,
        # and interpreter overhead. Running/queued pages reserve their share.
        images = {item[0]: item for item in page.get_images(full=True)}
        decoded = sum(item[2]*item[3]*4 for item in images.values())
        reservation = int(128*MIB + 2*PNG_BUDGET + 2*decoded + 12*largest)
        job = {'page': pno, 'pruned': self.layouts[pno],
               'key': layout_key(self.source_sha, pno, self.layouts[pno]),
               'detections': snapshot.values, 'skip_crops': skip_crops,
               'ocr': pno+1 in self.ocr_pages, 'reservation': reservation}
        return job, None

    def fill(self, pno, *, required=None):
        # Completed pages no longer occupy execution slots. Keep a bounded
        # second window to refill idle workers behind a slow earlier page.
        for index in range(pno, min(pno+self.window, len(self.layouts))):
            if index in self.pending or index in self.ready or index in self.completed:
                continue
            if len(self.pending) >= (self.pool_workers or self.workers):
                break
            if index not in self.plans:
                self.plans[index] = self.plan(index)
            job, cached = self.plans[index]
            if job is None:
                self.plans.pop(index)
                self.ready[index] = cached
                self.stats['cached_pages' if cached is not None else 'serial_layout_pages'] += 1
                continue
            # Limit speculative work to half of currently available memory;
            # keep room for the owner, GPU service, and other applications.
            available = self.memory_probe()
            capacity = min(self.budget, max(0, (available-SYSTEM_RESERVE)//2))
            if self.reserved()+job['reservation'] > capacity:
                if index != required:
                    # Future pages can be admitted after completed results
                    # release scratch reservations; do not permanently skip.
                    break
                self.stats['memory_skips'] += 1
                self.ready[index] = None
                self.plans.pop(index)
                continue
            if self.pool is None:
                # Pool eagerly starts every interpreter, even when the page
                # admission budget can run only one or two jobs. Size startup
                # by the same conservative per-page reservation to avoid a
                # 16-process burst disabling the entire pool under pressure.
                slots = max(1, (capacity-self.reserved())//job['reservation'])
                self.pool_workers = min(self.workers, slots)
                self.pool = mp.get_context('spawn').Pool(self.pool_workers, initializer=initialize_worker,
                    initargs=(self.source, self.source_sha, self.font_spec), maxtasksperchild=16)
                self.stats['pool_starts'] += 1
                self.stats['pool_workers'] = self.pool_workers
                self.stats['memory_limited_pool'] = self.pool_workers < self.workers
                print(f'[cpu] {self.pool_workers}/{self.workers} workers, '
                      f'budget {self.budget//MIB} MiB (memory-aware startup)', flush=True)
            self.pending[index] = (self.pool.apply_async(prepare_page, (job,)), job['reservation'], job['key'])
            self.plans.pop(index)
            if index >= pno+self.workers:
                self.stats['refill_jobs'] += 1
            self.stats['peak_pending_pages'] = max(self.stats['peak_pending_pages'], len(self.pending))
            self.stats['peak_reserved_mib'] = max(self.stats['peak_reserved_mib'], self.reserved()/MIB)
        self.stats['peak_buffered_pages'] = max(self.stats['peak_buffered_pages'],
            len(self.pending)+len(self.completed)+len(self.ready))

    def __call__(self, pno):
        if self.disabled:
            return None
        self.active_bytes = 0
        try:
            # overlay can skip existing-text / empty pages without calling us.
            for old in [key for key in self.ready if key < pno]:
                self.ready.pop(old)
            for mapping in (self.completed, self.plans):
                for old in [key for key in mapping if key < pno]:
                    mapping.pop(old)
            for old in [key for key in self.pending if key < pno]:
                # These pages should only be metadata-only, but never retain
                # an unconsumed future forever if a caller skips a cold page.
                self.fail(pno, f'skipped pending page {old+1}')
                return None
            if self.pressure():
                self.fail(pno, 'memory pressure')
                return None
            self.pump(pno, force=True, required=pno)
            if self.disabled:
                return None
            if pno in self.ready:
                return self.ready.pop(pno)
            if pno not in self.pending and pno not in self.completed:
                return None
            started = time.perf_counter()
            while pno not in self.completed:
                if self.pressure():
                    raise MemoryError('CPU pool memory pressure')
                result, reservation, key = self.pending[pno]
                try:
                    self.accept(pno, key, result.get(timeout=.1))
                except mp.TimeoutError:
                    if time.perf_counter()-started > self.timeout:
                        raise TimeoutError('CPU page preparation timeout')
                    self.pump(pno, force=True, required=pno)
                    if self.disabled:
                        return None
            self.stats['wait_seconds'] += time.perf_counter()-started
            value, retained = self.completed.pop(pno)
            blocks = {index: ([fitz.Rect(box) for box in boxes], chunks, images)
                      for index, (boxes, chunks, images) in value['blocks'].items()}
            for detection_key, boxes in value['detections'].items():
                self.store.save_detection(detection_key, boxes)
            self.active_bytes = retained
            self.stats['prepared_pages'] += 1
            self.stats['png_bytes'] += value['png_bytes']
            self.pump(pno+1, force=True)
            return blocks
        except Exception as error:
            self.fail(pno, error)
            return None
