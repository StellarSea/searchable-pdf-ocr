"""Production repair CPU geometry, resume, ownership and sticky recovery."""
import copy
import multiprocessing as mp
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import psutil
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_repair_prepare as cpu
import ocr_to_searchable_pdf as ocr


def broken_worker(job):
    raise RuntimeError('injected worker failure')


def dead_worker(job):
    os._exit(7)


def empty_worker(job):
    return dict(page=job['page'], key=job['key'], boxes={tuple(r): [] for r in job['rectangles']})


def geometry_worker(job):
    return dict(page=job['page'], key=job['key'],
                boxes={tuple(r): [list(r)] for r in job['rectangles']})


class RepairCPUTests(unittest.TestCase):
    def fixture(self, directory, count=4):
        source, pages = Path(directory)/'source.pdf', []
        with fitz.open() as doc:
            for index in range(count):
                page = doc.new_page(width=340, height=350)
                for y in range(30, 320, 16):
                    page.insert_text((20, y), 'Original ABC 123 and more sample text', fontsize=9)
                page.add_rect_annot(fitz.Rect(5, 5, 330, 340)).update()
                page.set_cropbox(fitz.Rect(3, 3, 337, 347))
                page.set_rotation(90*(index % 4))
                width, height = page.rect.width, page.rect.height
                pages.append({'width': width, 'height': height, 'parsing_res_list': [
                    {'block_content': 'Original', 'block_bbox': [.3, 1.7, width-1.2, height-2.4]},
                    {'block_content': 'Original', 'block_bbox': [3.1, 6.3, width-4.2, height-5.6]}]})
            doc.save(source)
        return source, pages

    def run_repair(self, source, original, workers, *, resume=False, preparer=None):
        pages, images, checkpoints, boxes, stats = copy.deepcopy(original), [], [], [], {}
        owner = threading.get_ident()

        def recognize(png, timeout=None):
            self.assertNotEqual(threading.get_ident(), owner)
            images.append(png)
            return 'Original recovered additional words' if len(images) % 2 else 'Changed 124'

        def checkpoint():
            self.assertEqual(threading.get_ident(), owner)
            checkpoints.append(copy.deepcopy(pages))

        def detected(page, rect, found):
            self.assertEqual(threading.get_ident(), owner)
            boxes.append((page.number, list(rect), [list(box) for box in found]))

        kwargs = dict(cpu_workers=workers, cpu_stats=stats, resume=resume, prefetch=True,
                      on_update=checkpoint, recognize_image=recognize, on_detection=detected)
        if preparer is None:
            fixed = ocr.repair_blocks(source, pages, **kwargs)
        else:
            preparer.pages = pages
            with patch.object(ocr, 'RepairCPUPreparer', return_value=preparer):
                fixed = ocr.repair_blocks(source, pages, **kwargs)
            stats = preparer.stats
        return (fixed, pages, images, checkpoints, boxes), stats

    def test_real_workers_exact_geometry_png_decisions_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            original = source.read_bytes()
            before, _ = self.run_repair(source, pages, 0)
            self.assertGreater(len(before[2]), 0, 'Fixture must actually exercise OCR requests')
            for workers in (2, 4):
                after, stats = self.run_repair(source, pages, workers)
                self.assertEqual(before, after)
                self.assertEqual(stats['prepared_pages'], 4)
                self.assertEqual(stats['pool_workers'], workers)
                self.assertIsNone(stats['fallback'])
                self.assertLessEqual(stats['peak_pending'], 2*workers)
            self.assertEqual(source.read_bytes(), original)

    def test_completed_resume_never_plans_renders_spawns_or_recognizes(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            for page in pages:
                for entry in page['parsing_res_list']:
                    entry['repair_audit'] = {'original': 'Original', 'accepted': False}
            with patch.object(cpu.RepairCPUPreparer, 'start', side_effect=AssertionError('planned')), \
                 patch.object(ocr, 'PageRaster', side_effect=AssertionError('rendered')):
                result, stats = self.run_repair(source, pages, 4, resume=True)
            self.assertEqual(result, (0, pages, [], [], []))
            self.assertEqual(stats['pool_starts'], 0)

    def test_partial_resume_preserves_audits_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            for entry in pages[0]['parsing_res_list']:
                entry['repair_audit'] = {'original': 'Original', 'candidate': 'saved', 'accepted': False}
            before, _ = self.run_repair(source, pages, 0, resume=True)
            after, stats = self.run_repair(source, pages, 4, resume=True)
            self.assertEqual(before, after)
            self.assertEqual(after[1][0], pages[0])
            self.assertEqual(stats['prepared_pages'], 3)

    def test_disabled_small_input_and_low_memory_never_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            for workers, selected, available in ((0, pages, 32*1024*cpu.MIB),
                    (4, pages[:1], 32*1024*cpu.MIB), (4, pages, cpu.SYSTEM_RESERVE)):
                with self.subTest(workers=workers, pages=len(selected), available=available):
                    prepared = cpu.RepairCPUPreparer(source, selected, workers=workers,
                        memory_probe=lambda: available,
                        pool_factory=lambda *a, **kw: self.fail('must not spawn'))
                    expected, _ = self.run_repair(source, selected, 0)
                    actual, stats = self.run_repair(source, selected, workers, preparer=prepared)
                    self.assertEqual(expected, actual)
                    self.assertEqual(stats['pool_starts'], 0)
                    self.assertIsNone(stats['fallback'])

    def test_memory_failure_after_progress_does_not_repeat_ocr_or_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            expected, _ = self.run_repair(source, pages, 0)
            prepared = cpu.RepairCPUPreparer(source, pages, workers=2,
                memory_probe=lambda: cpu.SYSTEM_RESERVE-1 if prepared.stats['prepared_pages'] else 32*1024*cpu.MIB)
            actual, stats = self.run_repair(source, pages, 2, preparer=prepared)
            self.assertEqual(expected, actual)
            self.assertEqual(stats['prepared_pages'], 1)
            self.assertEqual(stats['pool_starts'], 1)
            self.assertIn('memory pressure', stats['fallback'])
            self.assertIsNone(prepared.pool)

    def test_worker_exception_death_empty_and_partial_submit_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            expected, _ = self.run_repair(source, pages, 0)
            for worker_fn in (broken_worker, dead_worker, empty_worker):
                owned = []
                def factory(*args, **kwargs):
                    pool = mp.get_context('spawn').Pool(*args, **kwargs)
                    owned.extend(worker.pid for worker in pool._pool)
                    return pool
                prepared = cpu.RepairCPUPreparer(source, pages, workers=2, timeout=5,
                    worker_fn=worker_fn, pool_factory=factory)
                actual, stats = self.run_repair(source, pages, 2, preparer=prepared)
                self.assertEqual(expected, actual)
                self.assertTrue(all(not psutil.pid_exists(pid) for pid in owned))
                self.assertEqual(stats['pool_starts'], 1)
                if worker_fn is empty_worker:
                    self.assertGreater(stats['empty_retries'], 0)
                    self.assertIsNone(stats['fallback'])
                else:
                    self.assertIsNotNone(stats['fallback'])
                    if worker_fn is dead_worker:
                        self.assertIn('exited with code 7', stats['fallback'])
            owned = []
            def partial_factory(*args, **kwargs):
                pool = mp.get_context('spawn').Pool(*args, **kwargs)
                owned.extend(worker.pid for worker in pool._pool)
                submit, calls = pool.apply_async, []
                def apply_async(*a, **kw):
                    calls.append(1)
                    if len(calls) == 2:
                        raise RuntimeError('partial submit failed')
                    return submit(*a, **kw)
                pool.apply_async = apply_async
                return pool
            prepared = cpu.RepairCPUPreparer(source, pages, workers=2, pool_factory=partial_factory)
            actual, stats = self.run_repair(source, pages, 2, preparer=prepared)
            self.assertEqual(expected, actual)
            self.assertIn('partial submit failed', stats['fallback'])
            self.assertTrue(all(not psutil.pid_exists(pid) for pid in owned))

    def test_owner_callback_failure_and_interrupt_escape_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            for error in (OSError('checkpoint failed'), KeyboardInterrupt()):
                with self.assertRaises(type(error)):
                    with cpu.RepairCPUPreparer(source, pages, workers=2) as prepared, fitz.open(source) as doc:
                        def fail():
                            raise error
                        prepared.detect(doc[0], doc[0].rect, on_wait=fail)
                self.assertIsNone(prepared.stats['fallback'])
                self.assertIsNone(prepared.pool)

    def test_malformed_results_timeout_and_initializer_failure_recover(self):
        class Future:
            def __init__(self, result=None):
                self.result = result
            def get(self, timeout):
                if self.result is None:
                    raise mp.TimeoutError()
                return self.result
            def ready(self):
                return self.result is not None
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            for kind in ('identity', 'rectangle', 'nan', 'timeout'):
                with cpu.RepairCPUPreparer(source, pages, workers=2, timeout=.01) as prepared, fitz.open(source) as doc:
                    prepared.start(0)
                    job = prepared.jobs[0]
                    result = dict(page=0, key=job['key'], boxes={tuple(r): [[0, 0, 10, 10]] for r in job['rectangles']})
                    if kind == 'identity':
                        result['page'] = 999
                    elif kind == 'rectangle':
                        result['boxes'] = {}
                    elif kind == 'nan':
                        result['boxes'][tuple(job['rectangles'][0])] = [[0, 0, float('nan'), 1]]
                    prepared.pending[0] = Future(None if kind == 'timeout' else result)
                    rect = fitz.Rect(job['rectangles'][0])
                    expected = cpu.detect_lines(doc[0], rect)
                    self.assertEqual(prepared.detect(doc[0], rect), expected)
                    self.assertIsNotNone(prepared.stats['fallback'])
                    self.assertIsNone(prepared.pool)
            def initializer_failure(*args, **kwargs):
                source_arg, stamp, aa = kwargs['initargs']
                kwargs['initargs'] = source_arg, (-1, -1), aa
                return mp.get_context('spawn').Pool(*args, **kwargs)
            prepared = cpu.RepairCPUPreparer(source, pages, workers=2, pool_factory=initializer_failure)
            expected, _ = self.run_repair(source, pages, 0)
            actual, stats = self.run_repair(source, pages, 2, preparer=prepared)
            self.assertEqual(expected, actual)
            self.assertIn('source changed', stats['fallback'])

    def test_normal_worker_recycling_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory, count=40)
            with cpu.RepairCPUPreparer(source, pages, workers=2) as prepared, fitz.open(source) as doc:
                for pno, pruned in enumerate(pages):
                    page = doc[pno]
                    for entry in pruned['parsing_res_list']:
                        rect = cpu.repair_rect(page, entry['block_bbox'], (pruned['width'], pruned['height']))
                        self.assertEqual(prepared.detect(page, rect), cpu.detect_lines(page, rect))
                self.assertEqual(prepared.stats['prepared_pages'], 40)
                self.assertIsNone(prepared.stats['fallback'])
                self.assertEqual(prepared.stats['pool_starts'], 1)

    def test_large_last_page_does_not_limit_all_small_pages_to_one_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = Path(directory)/'mixed.pdf', []
            with fitz.open() as doc:
                for width, height in [(340, 350)]*6 + [(6000, 4000)]:
                    page = doc.new_page(width=width, height=height)
                    pages.append(dict(width=width, height=height, parsing_res_list=[
                        dict(block_content='', block_bbox=list(page.rect))]))
                doc.save(source)
            capacity = 5000*cpu.MIB
            with cpu.RepairCPUPreparer(source, pages, workers=4,
                    memory_probe=lambda: cpu.SYSTEM_RESERVE+2*capacity,
                    worker_fn=geometry_worker) as prepared, fitz.open(source) as doc:
                prepared.start(0)
                largest = max(job['reservation'] for job in prepared.jobs.values())
                self.assertEqual(capacity//largest, 1, 'Old admission would select one worker')
                self.assertEqual(prepared.stats['pool_workers'], 4)
                for page in doc:
                    self.assertEqual(prepared.detect(page, page.rect), [page.rect])
                self.assertEqual(prepared.stats['prepared_pages'], len(pages))
                self.assertIsNone(prepared.stats['fallback'])
                self.assertLessEqual(prepared.stats['peak_reserved_mib'], capacity/cpu.MIB)

    def test_weighted_admission_releases_ready_rasters_but_bounds_result_window(self):
        class Future:
            completed = False
            def ready(self):
                return self.completed
        class Pool:
            def apply_async(self, *args):
                return Future()
        prepared = cpu.RepairCPUPreparer('unused', [], workers=4)
        prepared.pool = Pool()
        prepared.stats['pool_workers'] = 4
        prepared.capacity = 4096*cpu.MIB
        prepared.jobs = {pno: dict(reservation=cpu.WORKER_BASE+extra*cpu.MIB)
                         for pno, extra in enumerate([1800, 1800] + [100]*12)}
        prepared.queue = list(prepared.jobs)
        prepared.fill()
        self.assertEqual(list(prepared.pending), [0], 'Two large rasters exceed capacity')
        prepared.pending[0].completed = True
        prepared.fill()
        self.assertEqual(list(prepared.pending), [0, 1, 2, 3])
        for future in prepared.pending.values():
            future.completed = True
        prepared.fill()
        self.assertEqual(len(prepared.pending), 8)
        for future in prepared.pending.values():
            future.completed = True
        prepared.fill()
        self.assertEqual(len(prepared.pending), 8, 'Ready geometry must remain bounded')
        self.assertLessEqual(prepared.stats['peak_reserved_mib'], 4096)
        self.assertLessEqual(prepared.stats['peak_running_pages'], 4)


if __name__ == '__main__':
    unittest.main()
