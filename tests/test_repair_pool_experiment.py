"""Independent PDF readers, exact geometry and owned-pool cleanup for the probe."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import psutil
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
import benchmark_repair_pool as probe


class RepairPoolExperimentTests(unittest.TestCase):
    def fixture(self, directory):
        path = Path(directory)/'source.pdf'
        with fitz.open() as doc:
            for index in range(3):
                page = doc.new_page(width=300, height=200)
                for y in (40, 60, 80, 100):
                    page.insert_text((30, y), 'Alpha beta 12345', fontsize=10)
                page.draw_rect(fitz.Rect(20, 20, 260, 120), width=.8)
                if index == 1:
                    page.set_rotation(90)
                if index == 2:
                    page.set_cropbox(fitz.Rect(10, 10, 290, 190))
                page.add_rect_annot(fitz.Rect(20, 130, 100, 150)).update()
            doc.save(path)
        pages = []
        with fitz.open(path) as doc:
            for page in doc:
                pages.append({'width': page.rect.width, 'height': page.rect.height,
                              'parsing_res_list': [{'block_content': 'text', 'block_bbox': list(page.rect)}]})
        return path, pages

    def test_independent_readers_keep_geometry_and_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            before = source.read_bytes()
            with probe.PreparedDetections(source, pages, 2) as prepared, fitz.open(source) as doc:
                for pno, rectangles, _ in probe.plan(source, pages):
                    page = doc[pno]
                    for rect in rectangles:
                        expected = probe.detect_lines(page, fitz.Rect(rect), raster=probe.PageRaster(page))
                        self.assertEqual(prepared.detect(page, fitz.Rect(rect)), expected)
                pids = [worker.pid for worker in prepared.pool._pool]
            self.assertEqual(prepared.stats['prepared_pages'], 3)
            self.assertLessEqual(prepared.stats['peak_pending'], 4)
            self.assertEqual(source.read_bytes(), before)
            self.assertTrue(all(not psutil.pid_exists(pid) for pid in pids))

    def test_caller_failure_cleans_only_owned_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            baseline = {process.pid for process in psutil.Process().children()}
            with self.assertRaisesRegex(RuntimeError, 'caller failed'):
                with probe.PreparedDetections(source, pages, 2) as prepared:
                    pids = [worker.pid for worker in prepared.pool._pool]
                    raise RuntimeError('caller failed')
            self.assertTrue(all(not psutil.pid_exists(pid) for pid in pids))
            self.assertTrue(baseline <= {process.pid for process in psutil.Process().children()})

    def test_pressure_before_start_never_spawns(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            with patch.object(probe.psutil, 'virtual_memory') as memory, patch.object(probe.mp, 'get_context') as context:
                memory.return_value.available = probe.RESERVE
                with self.assertRaises(MemoryError):
                    with probe.PreparedDetections(source, pages, 2):
                        self.fail('Must not admit a worker')
                context.assert_not_called()

    def test_invalid_limits_are_rejected(self):
        for workers in (0, -1, 9):
            with self.assertRaises(ValueError):
                probe.PreparedDetections('unused', [], workers)

    def test_timeout_and_wrong_page_are_hard_failures_with_cleanup(self):
        class Pending:
            def get(self, timeout):
                raise probe.mp.TimeoutError()

        class WrongPage:
            def get(self, timeout):
                return 999, {}

        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            for future, error in ((Pending(), TimeoutError), (WrongPage(), ValueError)):
                with self.assertRaises(error):
                    with probe.PreparedDetections(source, pages, 1, timeout=.02) as prepared, fitz.open(source) as doc:
                        pids = [worker.pid for worker in prepared.pool._pool]
                        prepared.pending[0] = future
                        prepared.detect(doc[0], doc[0].rect)
                self.assertTrue(all(not psutil.pid_exists(pid) for pid in pids))

    def test_runtime_pressure_closes_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            source, pages = self.fixture(directory)
            with self.assertRaises(MemoryError):
                with probe.PreparedDetections(source, pages, 1) as prepared, fitz.open(source) as doc:
                    pids = [worker.pid for worker in prepared.pool._pool]
                    with patch.object(probe.psutil, 'virtual_memory') as memory:
                        memory.return_value.available = probe.RESERVE-1
                        prepared.detect(doc[0], doc[0].rect)
            self.assertTrue(all(not psutil.pid_exists(pid) for pid in pids))


if __name__ == '__main__':
    unittest.main()
