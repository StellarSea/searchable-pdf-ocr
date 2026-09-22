"""Bounded CPU/HTTP overlap with a single PDF/SQLite owner."""
import copy
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_to_searchable_pdf as ocr
from ocr_storage import LineCache


def fixture(root):
    source = root/'source.pdf'
    values = [['First line', 'Second line'], ['Third line', 'Fourth line'], ['Fifth line', 'Sixth line']]
    blocks = []
    with fitz.open() as drawing:
        page = drawing.new_page(width=250, height=280)
        for index, lines in enumerate(values):
            y = 30+index*80
            page.insert_text((20, y), lines[0])
            page.insert_text((20, y+20), lines[1])
            blocks.append({'block_content': ' '.join(lines), 'block_bbox': [15, y-15, 160, y+30]})
        png = page.get_pixmap().tobytes('png')
    with fitz.open() as doc:
        page = doc.new_page(width=250, height=280)
        page.insert_image(page.rect, stream=png)
        doc.save(source)
    return source, [{'width': 250, 'height': 280, 'parsing_res_list': blocks}], values


class PrefetchTests(unittest.TestCase):
    def test_overlap_and_same_pixels_coordinates_keys_and_decisions(self):
        owner = threading.get_ident()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, layouts, values = fixture(root)
            reports, decisions = [], []
            prepared = threading.Event()
            actual_prefetch = ocr.LineRefiner.prefetch_images
            actual_pixmap = ocr.PageRaster.get_pixmap
            actual_save = LineCache.save_responses
            def prefetch(*args, **kwargs):
                self.assertEqual(threading.get_ident(), owner)
                result = actual_prefetch(*args, **kwargs)
                prepared.set()
                return result
            def pixmap(*args, **kwargs):
                self.assertEqual(threading.get_ident(), owner)
                return actual_pixmap(*args, **kwargs)
            def save(*args, **kwargs):
                self.assertEqual(threading.get_ident(), owner)
                return actual_save(*args, **kwargs)
            for enabled in (False, True):
                refiner = ocr.LineRefiner(root/f'{enabled}.json', {1}, automatic=True,
                                         source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
                calls = []
                def recognize(images, **kwargs):
                    index = len(calls)
                    calls.append(index)
                    if enabled:
                        self.assertNotEqual(threading.get_ident(), owner)
                        if index == 0:
                            self.assertTrue(prepared.wait(3), 'CPU did not prepare during HTTP wait')
                    else:
                        self.assertEqual(threading.get_ident(), owner)
                    return values[index]
                try:
                    with patch.object(ocr, 'recognize_lines', side_effect=recognize), \
                         patch.object(ocr.LineRefiner, 'prefetch_images', prefetch), \
                         patch.object(ocr.PageRaster, 'get_pixmap', pixmap), \
                         patch.object(LineCache, 'save_responses', save):
                        report = ocr.overlay(source, layouts, root/f'{enabled}.pdf', automatic=True,
                                             line_refiner=refiner, prefetch=enabled)
                    if enabled:
                        stats = report.pop('prefetch_stats')
                        self.assertEqual(stats['blocks'], 2)
                        self.assertEqual(stats['crops'], 4)
                        self.assertEqual(stats['errors'], 0)
                        self.assertLessEqual(stats['peak_buffer_bytes'], 64*1024*1024)
                    reports.append(report)
                    decisions.append(copy.deepcopy(refiner.cache['decisions']))
                    self.assertEqual(calls, [0, 1, 2])
                finally:
                    refiner.close()
            self.assertEqual(reports[0], reports[1])
            self.assertEqual(decisions[0], decisions[1])
            with fitz.open(source) as src, fitz.open(root/'False.pdf') as a, fitz.open(root/'True.pdf') as b:
                self.assertEqual(a[0].get_text('rawdict'), b[0].get_text('rawdict'))
                self.assertEqual(src[0].get_pixmap().samples, b[0].get_pixmap().samples)
                self.assertEqual(a[0].get_pixmap().samples, b[0].get_pixmap().samples)

    def test_speculative_failure_falls_back_without_restarting_or_changing_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, layouts, values = fixture(root)
            refiner = ocr.LineRefiner(root/'lines.json', {1}, automatic=True)
            try:
                with patch.object(ocr, 'recognize_lines', side_effect=values) as api, \
                     patch.object(refiner, 'prefetch_images', side_effect=RuntimeError('out of budget / render failure')):
                    report = ocr.overlay(source, layouts, root/'output.pdf', automatic=True, line_refiner=refiner)
                self.assertEqual(api.call_count, 3)
                self.assertEqual(report['prefetch_stats']['errors'], 2)
                self.assertTrue(all(d['accepted'] for d in refiner.cache['decisions']))
                self.assertEqual([d['candidate'] for d in refiner.cache['decisions']], values)
            finally:
                refiner.close()

    def test_budget_skips_render_and_warm_cache_starts_no_network_worker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, layouts, values = fixture(root)
            sha = hashlib.sha256(source.read_bytes()).hexdigest()
            refiner = ocr.LineRefiner(root/'lines.json', {1}, automatic=True, source_sha256=sha)
            try:
                with fitz.open(source) as doc:
                    raster = ocr.PageRaster(doc[0], sha, refiner.store)
                    with patch.object(raster, 'get_pixmap', side_effect=AssertionError('budget exceeded')):
                        self.assertEqual(refiner.prefetch_images(doc[0], 'First', [fitz.Rect(15, 15, 160, 40)], raster, budget=1), {})
                with patch.object(ocr, 'recognize_lines', side_effect=values):
                    ocr.overlay(source, layouts, root/'cold.pdf', automatic=True, line_refiner=refiner)
            finally:
                refiner.close()
            refiner = ocr.LineRefiner(root/'lines.json', {1}, automatic=True, source_sha256=sha)
            try:
                with patch.object(ThreadPoolExecutor, 'submit', side_effect=AssertionError('warm cache started work')), \
                     patch.object(ocr.PageRaster, 'get_pixmap', side_effect=AssertionError('warm cache rendered')):
                    report = ocr.overlay(source, layouts, root/'warm.pdf', automatic=True, line_refiner=refiner)
                self.assertEqual(report['prefetch_stats']['blocks'], 0)
            finally:
                refiner.close()

    def test_cli_switch_changes_scheduling_not_completion_identity(self):
        parser = ocr.build_parser()
        a, b = parser.parse_args(['book.pdf']), parser.parse_args(['book.pdf', '--no-prefetch'])
        self.assertFalse(a.no_prefetch)
        self.assertTrue(b.no_prefetch)
        self.assertEqual(ocr.run_options(a), ocr.run_options(b))

    def test_empty_speculative_detection_is_retried_at_normal_position(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, layouts, values = fixture(root)
            refiner = ocr.LineRefiner(root/'lines.json', {1}, automatic=True)
            actual_layout = ocr._block_layout
            attempts = 0
            def prepare(page, rect, structured_text, *args):
                nonlocal attempts
                if structured_text.startswith('Third'):
                    attempts += 1
                    if attempts == 1:
                        return [], None
                return actual_layout(page, rect, structured_text, *args)
            try:
                with patch.object(ocr, '_block_layout', side_effect=prepare), \
                     patch.object(ocr, 'recognize_lines', side_effect=values):
                    report = ocr.overlay(source, layouts, root/'output.pdf', automatic=True, line_refiner=refiner)
                self.assertEqual(attempts, 2)
                self.assertEqual(report['pages'][0]['inserted_lines'], 6)
                self.assertEqual([d['candidate'] for d in refiner.cache['decisions']], values)
            finally:
                refiner.close()

    def test_http_failure_joins_worker_and_preserves_published_audit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, layouts, _ = fixture(root)
            cache = root/'lines.json'
            store = LineCache(cache, True)
            store.append_decision({'original': 'published'})
            store.publish_decisions()
            store.close()
            refiner = ocr.LineRefiner(cache, {1}, automatic=True)
            try:
                with patch.object(ocr, 'recognize_lines', side_effect=RuntimeError('HTTP failure')) as api, \
                     self.assertRaisesRegex(RuntimeError, 'Line OCR interrupted'):
                    ocr.overlay(source, layouts, root/'output.pdf', automatic=True, line_refiner=refiner)
                self.assertEqual(api.call_count, 2)
                self.assertFalse((root/'output.pdf').exists())
                self.assertFalse(any(t.name.startswith('ocr-http') for t in threading.enumerate()))
            finally:
                refiner.close()
            with closing(sqlite3.connect(cache.with_suffix('.sqlite3'))) as db:
                rows = db.execute('SELECT value FROM decisions').fetchall()
                self.assertEqual([json.loads(r[0]) for r in rows], [{'original': 'published'}])


if __name__ == '__main__':
    unittest.main()
