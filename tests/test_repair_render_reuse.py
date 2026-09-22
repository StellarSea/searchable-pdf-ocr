"""Repair preparation keeps byte-identical OCR inputs and owner-side decisions."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr


class RepairRenderTests(unittest.TestCase):
    def fixture(self, root, rotation=0):
        source = root / 'source.pdf'
        with fitz.open() as doc:
            page = doc.new_page(width=350, height=460)
            for y in range(35, 400, 18):
                page.insert_text((25, y), 'Original ABC 123 and more sample text', fontsize=10)
            annot = page.add_rect_annot(fitz.Rect(15, 20, 330, 410))
            annot.update()
            page.set_cropbox(fitz.Rect(7, 9, 343, 447))
            page.set_rotation(rotation)
            width, height = page.rect.width, page.rect.height
            doc.save(source)
        pages = [{'width': width, 'height': height, 'parsing_res_list': [
            {'block_content': 'Original', 'block_bbox': [0.3, 1.7, width-1.2, height-2.4]},
            {'block_content': 'Original', 'block_bbox': [3.1, 6.3, width-4.2, height-5.6]},
        ]}]
        return source, pages

    def run_repair(self, source, original, reuse, prefetch=False, **kwargs):
        pages, images, decisions, boxes = copy.deepcopy(original), [], [], []
        owner = threading.get_ident()
        actual_detect = ocr.detect_lines

        def recognize(png, timeout=None):
            self.assertEqual(threading.get_ident() == owner, not prefetch)
            images.append(png)
            return 'Original recovered additional words' if len(images) == 1 else 'Changed 124'

        def checkpoint():
            self.assertEqual(threading.get_ident(), owner)
            decisions.append(copy.deepcopy(pages))

        def detect(page, rect, raster=None):
            found = actual_detect(page, rect, raster=raster)
            boxes.append([list(box) for box in found])
            # Quarter-turn text need not look like three horizontal lines;
            # force eligibility only after checking its exact detection boxes.
            return [fitz.Rect(0, 0, 200, 5)] * 4

        with patch.object(ocr, 'detect_lines', side_effect=detect):
            fixed = ocr.repair_blocks(source, pages, reuse_raster=reuse,
                                     recognize_image=recognize, on_update=checkpoint, prefetch=prefetch, **kwargs)
        return fixed, pages, images, decisions, boxes

    def test_same_png_coordinates_decisions_checkpoints_for_rotations_and_annotations(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), tempfile.TemporaryDirectory() as td:
                source, pages = self.fixture(Path(td), rotation)
                before = self.run_repair(source, pages, False)
                with patch.object(ocr, 'PageRaster', wraps=ocr.PageRaster) as raster:
                    after = self.run_repair(source, pages, True)
                self.assertEqual(before, after)
                self.assertEqual(len(after[2]), 2)
                self.assertEqual(after[0], 1)
                self.assertEqual(raster.call_count, 1)
                overlapped = self.run_repair(source, pages, True, prefetch=True)
                self.assertEqual(before, overlapped)

    def test_display_list_failure_uses_original_renderer(self):
        with tempfile.TemporaryDirectory() as td:
            source, pages = self.fixture(Path(td))
            before = self.run_repair(source, pages, False)
            with patch.object(ocr, 'PageRaster', side_effect=RuntimeError('display list failed')) as raster:
                after = self.run_repair(source, pages, True)
            self.assertEqual(before, after)
            self.assertEqual(raster.call_count, 1)

    def test_completed_resume_does_not_build_display_list_or_recognize(self):
        with tempfile.TemporaryDirectory() as td:
            source, pages = self.fixture(Path(td))
            for entry in pages[0]['parsing_res_list']:
                entry['repair_audit'] = {'original': 'Original', 'candidate': 'Original', 'accepted': False}
            with patch.object(ocr, 'PageRaster', side_effect=AssertionError('resume rendered')):
                result = self.run_repair(source, pages, True, resume=True)
            self.assertEqual(result, (0, pages, [], [], []))

    def test_api_failure_keeps_existing_checkpoint_and_resume_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            source, original = self.fixture(Path(td))
            results = []
            for reuse in (False, True):
                pages, checkpoints = copy.deepcopy(original), []
                def save():
                    checkpoints.append(copy.deepcopy(pages))
                with patch.object(ocr, 'detect_lines', return_value=[fitz.Rect(0, 0, 200, 5)] * 4), \
                     patch.object(ocr, 'ocr_image', side_effect=RuntimeError('offline')):
                    with self.assertRaisesRegex(RuntimeError, 'offline'):
                        ocr.repair_blocks(source, pages, reuse_raster=reuse, on_update=save)
                results.append((pages, checkpoints))
                self.assertEqual(len(checkpoints), 1)
                self.assertNotIn('repair_audit', pages[0]['parsing_res_list'][0])
                self.assertNotIn('repair_error', pages[0]['parsing_res_list'][1])
            self.assertEqual(results[0], results[1])

    def test_real_repair_loop_prepares_second_block_during_first_request(self):
        started, prepared = threading.Event(), threading.Event()
        owner, calls, detections = threading.get_ident(), [], []
        with tempfile.TemporaryDirectory() as td:
            source, pages = self.fixture(Path(td))
            def detect(page, rect, raster=None):
                self.assertEqual(threading.get_ident(), owner)
                detections.append(list(rect))
                if len(detections) == 2:
                    try:
                        self.assertTrue(started.wait(5), 'Previous OCR never started')
                    finally:
                        prepared.set()
                return [fitz.Rect(0, 0, 200, 5)] * 4
            def recognize(png, timeout=None):
                self.assertNotEqual(threading.get_ident(), owner)
                calls.append(png)
                if len(calls) == 1:
                    started.set()
                    self.assertTrue(prepared.wait(5), 'Repair waited before preparing next block')
                return 'Original with recovered additional text'
            with patch.object(ocr, 'detect_lines', side_effect=detect):
                fixed = ocr.repair_blocks(source, pages, prefetch=True, recognize_image=recognize,
                                         on_update=lambda: self.assertEqual(threading.get_ident(), owner))
            self.assertEqual(fixed, 2)
            self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
