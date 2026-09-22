"""Lossless render reuse and pre-render cache regression checks (no OCR server)."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr
import ocr_render


class PerformanceTests(unittest.TestCase):
    def test_display_list_matches_pixels_png_and_line_coordinates(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), fitz.open() as doc:
                page = doc.new_page(width=320, height=240)
                page.insert_text((31, 51), 'CHAPTER 01 thin text', fontsize=9)
                page.insert_text((31, 73), 'Second line 123.45', fontsize=12)
                page.draw_rect(fitz.Rect(30, 85, 230, 105), color=(0.2, 0.4, 0.6))
                annot = page.add_rect_annot(fitz.Rect(40, 120, 130, 140))
                annot.update()
                page.set_cropbox(fitz.Rect(11, 13, 290, 220))
                page.set_rotation(rotation)
                raster = ocr.PageRaster(page)
                clip = fitz.Rect(0.3, 1.7, page.rect.width-2.4, page.rect.height-3.6)
                for cs in (fitz.csGRAY, fitz.csRGB):
                    options = dict(matrix=fitz.Matrix(ocr.ZOOM, ocr.ZOOM),
                                   colorspace=cs, alpha=False, clip=clip)
                    before, after = page.get_pixmap(**options), raster.get_pixmap(**options)
                    self.assertEqual(before.irect, after.irect)
                    self.assertEqual(before.samples, after.samples)
                    self.assertEqual(before.tobytes('png'), after.tobytes('png'))
                self.assertEqual(ocr.detect_lines(page, clip),
                                 ocr.detect_lines(page, clip, raster=raster))

    def test_persistent_early_hits_keep_decisions_and_skip_rendering(self):
        for automatic in (False, True):
            with self.subTest(automatic=automatic), tempfile.TemporaryDirectory() as td, fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((20, 30), 'First line')
                boxes = [fitz.Rect(15, 15, 140, 35), fitz.Rect(15, 40, 140, 60)]
                values = ['First line', 'Second line']
                path = Path(td)/'lines.json'
                refiner = ocr.LineRefiner(path, {1}, automatic=automatic)
                raster = ocr.PageRaster(page, 'source-A')
                with patch.object(ocr, 'recognize_lines', return_value=values):
                    first = refiner.refine(page, ''.join(values), boxes, values, raster=raster)
                decision = refiner.cache['decisions'][-1]
                refiner.close()
                refiner = ocr.LineRefiner(path, {1}, automatic=automatic)
                try:
                    with patch.object(raster, 'get_pixmap', side_effect=AssertionError('rendered a cache hit')), \
                         patch.object(ocr, 'recognize_lines', side_effect=AssertionError('called OCR')):
                        self.assertEqual(refiner.refine(page, ''.join(values), boxes, values, raster=raster), first)
                    self.assertEqual(refiner.cache['decisions'][-1], decision)
                    self.assertEqual(refiner.stats, {'rendered_crops': 0, 'early_cache_hits': 2})
                finally:
                    refiner.close()

    def test_crop_identity_invalidates_each_pixel_or_engine_input(self):
        with fitz.open() as doc:
            page = doc.new_page()
            crop = fitz.Rect(10, 20, 100, 40)
            raster = ocr.PageRaster(page, 'source-A')
            key = raster.crop_key(crop, 'engine-A')
            self.assertNotEqual(key, ocr.PageRaster(page, 'source-B').crop_key(crop, 'engine-A'))
            self.assertNotEqual(key, raster.crop_key(crop+(0, 0, 0.1, 0), 'engine-A'))
            self.assertNotEqual(key, raster.crop_key(crop, 'engine-B'))
            with patch.object(ocr_render, 'ZOOM', ocr_render.ZOOM+0.1):
                self.assertNotEqual(key, raster.crop_key(crop, 'engine-A'))
            with patch.object(fitz, 'version', ('changed', 'renderer', None)):
                self.assertNotEqual(key, ocr.PageRaster(page, 'source-A').crop_key(crop, 'engine-A'))
            page.set_rotation(90)
            self.assertNotEqual(key, ocr.PageRaster(page, 'source-A').crop_key(crop, 'engine-A'))
            page.set_rotation(0)
            page.set_cropbox(fitz.Rect(1, 1, 300, 400))
            self.assertNotEqual(key, ocr.PageRaster(page, 'source-A').crop_key(crop, 'engine-A'))
            page = doc.new_page()
            self.assertNotEqual(key, ocr.PageRaster(page, 'source-A').crop_key(crop, 'engine-A'))
            self.assertIsNone(ocr.PageRaster(page).crop_key(crop, 'engine-A'))

    def test_old_image_cache_is_reused_then_indexed_without_api(self):
        with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
            page = doc.new_page()
            rect = fitz.Rect(10, 10, 100, 30)
            crop = rect+(-1, -0.7, 1, 0.7)
            png = page.get_pixmap(matrix=fitz.Matrix(ocr.ZOOM, ocr.ZOOM), clip=crop,
                                  colorspace=fitz.csRGB, alpha=False).tobytes('png')
            settings = json.dumps({'api': ocr.LINE_API, 'engine': ocr.LINE_ENGINE}, sort_keys=True)
            image_key = hashlib.sha256(settings.encode()+png).hexdigest()
            path = Path(td)/'lines.json'
            ocr.atomic_json(path, {'responses': {image_key: 'Hello'}})
            refiner = ocr.LineRefiner(path, {1}, automatic=True)
            raster = ocr.PageRaster(page, 'source-A')
            try:
                with patch.object(ocr, 'recognize_lines', side_effect=AssertionError('old cache lost')):
                    self.assertEqual(refiner.refine(page, 'Hello', [rect], ['Hello'], raster=raster), ['Hello'])
                    self.assertEqual(refiner.stats['rendered_crops'], 1)
                    with patch.object(raster, 'get_pixmap', side_effect=AssertionError('not indexed')):
                        refiner.refine(page, 'Hello', [rect], ['Hello'], raster=raster)
                self.assertEqual(refiner.cache['decisions'][-1]['image_keys'], [image_key])
            finally:
                refiner.close()

    def test_dangling_index_does_not_return_missing_response(self):
        with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
            page = doc.new_page()
            rect = fitz.Rect(10, 10, 100, 30)
            raster = ocr.PageRaster(page, 'source-A')
            refiner = ocr.LineRefiner(Path(td)/'lines.json', {1}, automatic=True)
            try:
                with patch.object(ocr, 'recognize_lines', return_value=['Hello']):
                    refiner.refine(page, 'Hello', [rect], ['Hello'], raster=raster)
                refiner.db.execute('DELETE FROM responses')
                refiner.db.commit()
                with patch.object(ocr, 'recognize_lines', return_value=['Hello']) as api:
                    refiner.refine(page, 'Hello', [rect], ['Hello'], raster=raster)
                    api.assert_called_once()
                self.assertEqual(refiner.stats['early_cache_hits'], 0)
            finally:
                refiner.close()


if __name__ == '__main__':
    unittest.main()
