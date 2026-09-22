"""Experimental read-only PDF preparation pool. Does not alter the OCR pipeline.

Each spawned process opens its own PDF and font. Results include PNG bytes, so
measurements include real inter-process transfer, not just tiny hash messages.
The parent alone orders results and writes diagnostic JSON; no GPU/SQLite calls.
"""
import argparse
import atexit
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import statistics
import sys
import threading
import time

import psutil
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_to_searchable_pdf as ocr
from ocr_storage import atomic_json

_document = None
_font = None
_source_sha = None
MAX_PAGE_PNG_BYTES = 64*1024*1024


class DetectionCollector:
    """Process-local exact detections; the PDF owner may persist them later."""
    def __init__(self):
        self.values = {}

    def detection(self, key):
        return self.values.get(key)

    def save_detection(self, key, boxes):
        self.values[key] = [list(box) for box in boxes]


def close_worker():
    global _document, _font
    if _document is not None:
        _document.close()
    _document = _font = None


def initialize_worker(source, source_sha, font_path, barrier=None):
    global _document, _font, _source_sha
    close_worker()
    _document = fitz.open(source)
    _font = fitz.Font(fontfile=font_path)
    _source_sha = source_sha
    if os.name == 'nt' and barrier is not None:
        try:
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except psutil.AccessDenied:
            pass
    atexit.register(close_worker)
    if barrier is not None:
        barrier.wait(timeout=60)


def ping_worker():
    return os.getpid()


def render_page(job):
    """Use the production layout path and exact RGB crop/engine key rules."""
    pno, pruned = job
    page = _document[pno]
    started = time.perf_counter()
    blocks = []
    retained = 0
    try:
        if page.get_text().strip():
            return {'page': pno, 'blocks': [], 'existing_text': True, 'png_bytes': 0}
        detections = DetectionCollector()
        raster = ocr.PageRaster(page, _source_sha, detections)
        source_blocks = ocr._find_blocks(pruned)
        size = ocr._find_size(pruned)
        if size and size[0] > 0 and size[1] > 0:
            sx, sy = page.rect.width/size[0], page.rect.height/size[1]
        else:
            sx = page.rect.width/(max((b[2] for _, b in source_blocks), default=1) or 1)
            sy = page.rect.height/(max((b[3] for _, b in source_blocks), default=1) or 1)
        reviewed = {(b['block_content'], tuple(b['block_bbox']))
                    for b in pruned.get('parsing_res_list', []) if b.get('block_label') == 'reviewed_line'}
        for block_index, (raw, bbox) in enumerate(source_blocks):
            raw = raw.replace(ocr.REPLACEMENT_CHAR, '')
            text = ocr.strip_html(raw)
            rect = fitz.Rect(bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy) & page.rect
            if not text.strip() or rect.is_empty or min(rect.width, rect.height) < 1:
                continue
            boxes, chunks = ocr._block_layout(page, rect, raw, (text, tuple(bbox)) in reviewed,
                                              False, _font, raster)
            crops = []
            if len(boxes) > 1 and not ocr.HTML_TAG_RE.search(raw) and '\\' not in raw and '$$' not in raw:
                engine = ocr.LINE_ENGINE_KOREAN if ocr.line_language(raw) else ocr.LINE_ENGINE
                settings = json.dumps({'api': ocr.LINE_API, 'engine': engine}, sort_keys=True)
                padding = ocr.page_coordinate_scale(page)
                for box in boxes:
                    crop = fitz.Rect(box.x0-padding, box.y0-.7*padding,
                                     box.x1+padding, box.y1+.7*padding) & page.rect
                    pix = raster.get_pixmap(matrix=fitz.Matrix(ocr.ZOOM, ocr.ZOOM), clip=crop,
                                            colorspace=fitz.csRGB, alpha=False)
                    png = pix.tobytes('png')
                    retained += len(png)
                    if retained > MAX_PAGE_PNG_BYTES:
                        raise RuntimeError(f'Page {pno+1} exceeds diagnostic PNG budget; no pixels downsampled')
                    crops.append({'rect': list(crop), 'png': png,
                                  'crop_key': raster.crop_key(crop, settings),
                                  'image_key': hashlib.sha256(settings.encode()+png).hexdigest()})
            blocks.append({'index': block_index, 'text': raw, 'rects': [list(r) for r in boxes],
                           'chunks': chunks, 'crops': crops})
        return {'page': pno, 'blocks': blocks, 'existing_text': False, 'png_bytes': retained,
                'detections': detections.values,
                'worker_seconds': time.perf_counter()-started, 'pid': os.getpid()}
    finally:
        # Bound MuPDF's decoded-image store between tasks. The same policy is
        # used in the sequential reference and every worker-count benchmark.
        fitz.TOOLS.store_shrink(100)


def signature(result):
    """Check bytes after actual IPC transfer, then release the PNG payloads."""
    result.pop('worker_seconds', None)
    result.pop('pid', None)
    for block in result['blocks']:
        for crop in block['crops']:
            png = crop.pop('png')
            crop['png_sha256'] = hashlib.sha256(png).hexdigest()
    return result


def run_bounded(pool, jobs, limit):
    pending, completed = {}, [None]*len(jobs)
    iterator = iter(enumerate(jobs))
    def submit_next():
        item = next(iterator, None)
        if item is not None:
            index, job = item
            pending[pool.submit(render_page, job)] = index
    for _ in range(min(limit, len(jobs))):
        submit_next()
    while pending:
        done, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            index = pending.pop(future)
            result = future.result()
            completed[index] = signature(result)
            del result
        # Drop completed Future references before adding more PNG-bearing work.
        count = len(done)
        done.clear()
        future = None
        for _ in range(count):
            submit_next()
    return completed


class MemoryMonitor:
    """Sample only this diagnostic process tree; RSS sums can double-count sharing."""
    def __init__(self):
        self.stop = threading.Event()
        self.parent = psutil.Process()
        self.peak_rss = self.peak_child_rss = 0
        self.minimum_available = psutil.virtual_memory().available
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        while not self.stop.is_set():
            children = 0
            try:
                for process in self.parent.children(recursive=True):
                    try:
                        children += process.memory_info().rss
                    except psutil.Error:
                        pass
                self.peak_child_rss = max(self.peak_child_rss, children)
                self.peak_rss = max(self.peak_rss, self.parent.memory_info().rss+children)
                self.minimum_available = min(self.minimum_available, psutil.virtual_memory().available)
            except psutil.Error:
                pass
            self.stop.wait(.05)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()


def benchmark(args):
    if args.out.exists():
        raise ValueError('Use a new diagnostic output directory')
    args.out.mkdir(parents=True)
    numbers = [int(n) for n in args.pages.split(',')]
    if len(numbers) != len(set(numbers)):
        raise ValueError('Page numbers must be unique')
    layouts = json.loads(args.layout.read_text(encoding='utf-8'))
    jobs = [(n-1, layouts[n-1] if isinstance(layouts, list) else layouts['pages'][str(n-1)]['result']) for n in numbers]
    with args.source.open('rb') as stream:
        source_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    initialize_worker(str(args.source), source_sha, str(args.font))
    try:
        started = time.perf_counter()
        reference = [signature(render_page(job)) for job in jobs]
        reference_seconds = time.perf_counter()-started
    finally:
        close_worker()
    atomic_json(args.out/'reference.json', reference)
    report = {'pages': numbers, 'source_sha256': source_sha, 'reference_seconds': reference_seconds,
              'rounds': args.rounds, 'runs': [], 'skipped': [],
              'scope': 'uncached PDF layout + RGB PNG + IPC; no OCR inference, insertion, or final verification',
              'logical_cpus': psutil.cpu_count(), 'physical_cpus': psutil.cpu_count(logical=False)}
    print(json.dumps({'reference_seconds': reference_seconds}), flush=True)
    ctx = mp.get_context('spawn')
    peak_per_worker = None
    for count in args.workers:
        available = psutil.virtual_memory().available
        estimate = count*(peak_per_worker or 1024**3)*1.3
        if available-estimate < 4*1024**3:
            report['skipped'].append({'workers': count, 'reason': 'preserve at least 4 GiB available memory'})
            continue
        started = time.perf_counter()
        with MemoryMonitor() as memory:
            with ProcessPoolExecutor(max_workers=count, mp_context=ctx, initializer=initialize_worker,
                                     initargs=(str(args.source), source_sha, str(args.font), ctx.Barrier(count))) as pool:
                ready = [pool.submit(ping_worker) for _ in range(count)]
                for future in ready:
                    future.result(timeout=90)
                startup = time.perf_counter()-started
                elapsed = []
                for round_index in range(args.rounds):
                    started_round = time.perf_counter()
                    result = run_bounded(pool, jobs, count)
                    duration = time.perf_counter()-started_round
                    if result != reference:
                        atomic_json(args.out/f'mismatch-{count}-{round_index}.json', result)
                        raise AssertionError('Different boxes/chunks/PNG bytes/cache keys')
                    elapsed.append(duration)
                    print(json.dumps({'workers': count, 'round': round_index, 'seconds': duration, 'exact': True}), flush=True)
        if count == 1:
            peak_per_worker = memory.peak_child_rss
        entry = {'workers': count, 'startup_seconds': startup, 'seconds': elapsed,
                 'median_seconds': statistics.median(elapsed), 'exact': True,
                 'peak_tree_rss_mib': memory.peak_rss/1024**2,
                 'peak_child_rss_mib': memory.peak_child_rss/1024**2,
                 'minimum_system_available_mib': memory.minimum_available/1024**2}
        report['runs'].append(entry)
        atomic_json(args.out/'comparison.json', report)
        print(json.dumps(entry), flush=True)
    atomic_json(args.out/'comparison.json', report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('layout', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--font', type=Path, default=Path('C:/Windows/Fonts/malgun.ttf'))
    parser.add_argument('--pages', default='6,7,29,38,66,147,166,188')
    parser.add_argument('--workers', nargs='+', type=int, default=[1,2,4,8])
    parser.add_argument('--rounds', type=int, default=3)
    options = parser.parse_args()
    if options.rounds < 1 or any(n < 1 or n > 8 for n in options.workers) or options.workers[0] != 1:
        parser.error('Use 1-8 workers, start with 1 for memory calibration, and at least one round')
    benchmark(options)
