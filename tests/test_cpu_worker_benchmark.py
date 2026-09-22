"""Offline guarantees for the experimental, non-production CPU pool."""
from concurrent.futures import ProcessPoolExecutor
import copy
import hashlib
import multiprocessing as mp
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
import benchmark_cpu_workers as bench
from benchmark_cpu_overlay import PreparedPages, assert_pdf_equal
from benchmark_prefetch import comparable


class CPUWorkerBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(bench.close_worker)
        root = Path(self.directory.name)
        self.source = root/'scan.pdf'
        self.font = root/'font.ttf'
        self.font.write_bytes(fitz.Font('helv').buffer)
        with fitz.open() as drawing:
            page = drawing.new_page(width=180, height=150)
            for y, text in [(30, 'First sample line'), (60, 'Second sample line'), (90, 'Third sample line')]:
                page.insert_text((15, y), text, fontsize=12)
            png = page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes('png')
        with fitz.open() as document:
            for _ in range(3):
                page = document.new_page(width=180, height=150)
                page.insert_image(page.rect, stream=png)
            document.save(self.source)
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        layout = {'width': 180, 'height': 150, 'parsing_res_list': [{
            'block_content': 'First sample line Second sample line Third sample line',
            'block_bbox': [10, 10, 170, 105], 'block_label': 'text'}]}
        # Deliberately non-numeric order: completion must not reorder pages.
        self.jobs = [(2, layout), (0, layout), (1, layout)]

    def initialize(self):
        bench.initialize_worker(str(self.source), self.sha, str(self.font))

    def test_spawned_preparation_preserves_bytes_boxes_keys_and_order(self):
        self.initialize()
        reference = [bench.signature(bench.render_page(job)) for job in self.jobs]
        self.assertTrue(all(page['png_bytes'] > 0 for page in reference))
        bench.close_worker()
        ctx = mp.get_context('spawn')
        with ProcessPoolExecutor(max_workers=2, mp_context=ctx, initializer=bench.initialize_worker,
                                 initargs=(str(self.source), self.sha, str(self.font), ctx.Barrier(2))) as pool:
            result = bench.run_bounded(pool, self.jobs, 2)
        self.assertEqual(result, reference)
        self.assertEqual([page['page'] for page in result], [2, 0, 1])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.sha)

    def test_budget_excess_fails_without_downsampling(self):
        self.initialize()
        with patch.object(bench, 'MAX_PAGE_PNG_BYTES', 0):
            with self.assertRaisesRegex(RuntimeError, 'no pixels downsampled'):
                bench.render_page(self.jobs[0])

    def test_signature_hashes_transferred_bytes_and_removes_runtime_fields(self):
        result = {'page': 0, 'pid': 99, 'worker_seconds': 1.2,
                  'blocks': [{'crops': [{'png': b'exact image'}]}]}
        self.assertEqual(bench.signature(result), {'page': 0, 'blocks': [{'crops': [{
            'png_sha256': hashlib.sha256(b'exact image').hexdigest()}]}]})

    def test_real_overlay_with_process_pool_preserves_decisions_and_warm_cache(self):
        font = Path('C:/Windows/Fonts/malgun.ttf')
        if not font.exists():
            self.skipTest('This overlay fixture requires the production Windows font')
        root = Path(self.directory.name)
        layouts = [job[1] for job in self.jobs]
        reference_report = reference_decisions = None
        for workers in (0, 2):
            cache = root/f'cache-{workers}.json'
            refiner = bench.ocr.LineRefiner(cache, {1, 2, 3}, automatic=True, source_sha256=self.sha)
            try:
                with patch.object(bench.ocr, 'recognize_lines', side_effect=lambda images, **kw: ['?']*len(images)):
                    if workers:
                        with PreparedPages(self.source, layouts, refiner, workers, font) as prepared:
                            report = bench.ocr.overlay(self.source, layouts, root/'pooled.pdf',
                                                       line_refiner=refiner, automatic=True, prepared_pages=prepared)
                        self.assertEqual(prepared.stats['pages'], 3)
                        self.assertEqual(prepared.stats['fallbacks'], [])
                        self.assertLessEqual(prepared.stats['peak_pending_pages'], workers)
                        self.assertEqual(comparable(report), reference_report)
                        self.assertEqual(refiner.cache['decisions'], reference_decisions)
                        assert_pdf_equal(root/'baseline.pdf', root/'pooled.pdf')
                    else:
                        report = bench.ocr.overlay(self.source, layouts, root/'baseline.pdf',
                                                   line_refiner=refiner, automatic=True)
                        reference_report = comparable(report)
                        reference_decisions = copy.deepcopy(refiner.cache['decisions'])
            finally:
                refiner.close()
            if workers:
                refiner = bench.ocr.LineRefiner(cache, {1, 2, 3}, automatic=True, source_sha256=self.sha)
                try:
                    with patch.object(bench.ocr, 'recognize_lines', side_effect=AssertionError('warm HTTP')):
                        warm = bench.ocr.overlay(self.source, layouts, root/'warm.pdf',
                                                 line_refiner=refiner, automatic=True)
                    self.assertEqual(comparable(warm), reference_report)
                    self.assertEqual(refiner.cache['decisions'], reference_decisions)
                    assert_pdf_equal(root/'baseline.pdf', root/'warm.pdf')
                finally:
                    refiner.close()

    def test_failed_prepared_page_uses_original_overlay_path(self):
        root = Path(self.directory.name)
        layouts = [self.jobs[0][1]]*3
        refiner = bench.ocr.LineRefiner(root/'failure.json', {1, 2, 3}, automatic=True, source_sha256=self.sha)
        try:
            prepared = PreparedPages(self.source, layouts, refiner, 2, self.font)
            class FailedFuture:
                def result(self):
                    raise RuntimeError('injected worker failure')
            prepared.pending = {index: FailedFuture() for index in range(3)}
            prepared.next_page = len(layouts)
            with patch.object(bench.ocr, 'recognize_lines', side_effect=lambda images, **kw: ['?']*len(images)):
                report = bench.ocr.overlay(self.source, layouts, root/'fallback.pdf',
                                           line_refiner=refiner, automatic=True, prepared_pages=prepared)
            self.assertEqual(len(prepared.stats['fallbacks']), 3)
            self.assertTrue(all(page['inserted_lines'] > 0 for page in report['pages']))
            self.assertEqual(len(refiner.cache['decisions']), 3)
        finally:
            refiner.close()

    def test_short_sample_caps_workers_and_closes_pool_on_caller_failure(self):
        root = Path(self.directory.name)
        refiner = bench.ocr.LineRefiner(root/'short.json', {1}, automatic=True, source_sha256=self.sha)
        try:
            prepared = PreparedPages(self.source, [self.jobs[0][1]], refiner, 8, self.font)
            self.assertEqual(prepared.workers, 1)
            with self.assertRaisesRegex(RuntimeError, 'caller failure'):
                with prepared:
                    self.assertTrue(prepared(0))
                    raise RuntimeError('caller failure')
            self.assertEqual(prepared.pending, {})
            self.assertTrue(prepared.pool._shutdown_thread)
        finally:
            refiner.close()


if __name__ == '__main__':
    unittest.main()
