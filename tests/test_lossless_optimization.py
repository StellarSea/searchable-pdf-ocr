"""Optimization contracts: exact pixels/boxes, retryable failures, bounded work."""
import asyncio
import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
from ocr_storage import BoundaryCache, LineCache, atomic_json
import ocr_render as render
import ocr_api
import ocr_boundary
import ocr_to_searchable_pdf as ocr
from test_line_worker_tuning import load_server


def old_trim(image, threshold=245):
    gray = np.dot(image[..., :3], (.299, .587, .114))
    ys, xs = np.nonzero(gray < threshold)
    if not xs.size:
        return image
    return image[max(0, int(ys.min())-2):min(image.shape[0], int(ys.max())+3),
                 max(0, int(xs.min())-2):min(image.shape[1], int(xs.max())+3)]


def old_split(server, image):
    image = old_trim(image)
    height, width = image.shape[:2]
    max_width = max(96, int(round(height * server.MAX_SEGMENT_RATIO)))
    if width <= max_width:
        return [image], []
    gray = np.dot(image[..., :3], (.299, .587, .114))
    blank = (gray < 225).sum(axis=0) <= max(1, height // 100)
    word_gap = max(2, int(round(height * server.WORD_GAP_RATIO)))
    runs = server._blank_runs(blank, 0, width)
    segments, separators, left = [], [], 0
    while width-left > max_width:
        usable = [r for r in runs if r[0] > left+8]
        before = [r for r in usable if (r[0]+r[1])//2 <= left+max_width]
        if before:
            a, b = before[-1]
        elif usable:
            a, b = usable[0]
        else:
            break
        cut = (a+b)//2
        segments.append(old_trim(image[:, left:cut]))
        separators.append(' ' if b-a >= word_gap else '')
        left = cut
    segments.append(old_trim(image[:, left:]))
    return segments, separators


class LosslessPreprocessingTests(unittest.TestCase):
    def test_exact_segments_on_random_thresholds_noise_and_blank_gaps(self):
        server = load_server()
        rng = np.random.default_rng(812)
        for i in range(100):
            height, width = int(rng.integers(1, 90)), int(rng.integers(1, 1400))
            image = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
            if i % 2:
                image[rng.random((height, width)) > .15] = 255
            if i % 3 == 0:
                for x in range(0, width, 50):
                    image[:, x:x+int(rng.integers(2, 20))] = 255
            if i % 10 == 0:
                image[:] = [224, 225, 244, 245, 255][i//10 % 5]
            # Include non-contiguous views; PNG input is contiguous but helpers
            # are also used by diagnostic callers on sliced arrays.
            if i % 7 == 0:
                image = image[:, ::2]
            np.testing.assert_array_equal(old_trim(image), server._trim(image))
            before, after = old_split(server, image), server.split_long_line(image)
            self.assertEqual(before[1], after[1])
            self.assertEqual(len(before[0]), len(after[0]))
            for a, b in zip(before[0], after[0]):
                np.testing.assert_array_equal(a, b)


class DetectionCacheTests(unittest.TestCase):
    def test_warm_overlay_keeps_pixels_text_coordinates_and_decisions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root/'source.pdf'
            with fitz.open() as drawing:
                page = drawing.new_page(width=200, height=120)
                page.insert_text((20, 35), 'First line')
                page.insert_text((20, 55), 'Second line')
                png = page.get_pixmap().tobytes('png')
            with fitz.open() as scanned:
                page = scanned.new_page(width=200, height=120)
                page.insert_image(page.rect, stream=png)
                scanned.save(source)
            pages = [{'width': 200, 'height': 120, 'parsing_res_list': [
                {'block_content': 'First line Second line', 'block_bbox': [15, 20, 130, 65]}]}]
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            decisions, reports = [], []
            for phase in ('cold', 'warm'):
                refiner = ocr.LineRefiner(root/'lines.json', {1}, automatic=True, source_sha256=source_sha)
                try:
                    with patch.object(ocr, 'recognize_lines', return_value=['First line', 'Second line']):
                        if phase == 'warm':
                            with patch.object(render.PageRaster, 'get_pixmap', side_effect=AssertionError('warm render')):
                                report = ocr.overlay(source, pages, root/f'{phase}.pdf', line_refiner=refiner, automatic=True)
                        else:
                            report = ocr.overlay(source, pages, root/f'{phase}.pdf', line_refiner=refiner, automatic=True)
                    self.assertEqual(report['detection_cache_stats'],
                                     {'hits': 1 if phase == 'warm' else 0, 'misses': 0 if phase == 'warm' else 1})
                    for key in ('line_cache_stats', 'detection_cache_stats', 'prefetch_stats'):
                        report.pop(key)
                    decisions.append(copy.deepcopy(refiner.cache['decisions']))
                    reports.append(report)
                finally:
                    refiner.close()
            self.assertEqual(reports[0], reports[1])
            self.assertEqual(decisions[0], decisions[1])
            with fitz.open(source) as src, fitz.open(root/'cold.pdf') as cold, fitz.open(root/'warm.pdf') as warm:
                self.assertEqual(cold[0].get_text('rawdict'), warm[0].get_text('rawdict'))
                self.assertEqual(src[0].get_pixmap().samples, cold[0].get_pixmap().samples)
                self.assertEqual(cold[0].get_pixmap().samples, warm[0].get_pixmap().samples)

    def test_persistent_boxes_and_empty_results_skip_render(self):
        for automatic in (False, True):
            with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
                page = doc.new_page(width=200, height=200)
                page.insert_text((20, 30), 'CHAPTER 01')
                rects = [fitz.Rect(10, 10, 170, 80), fitz.Rect(10, 120, 170, 170)]
                expected = [render.detect_lines(page, r) for r in rects]
                self.assertTrue(expected[0])
                self.assertEqual(expected[1], [])
                path = Path(td) / 'cache.json'
                for attempt in range(2):
                    store = LineCache(path, automatic)
                    try:
                        raster = render.PageRaster(page, 'source', store)
                        if attempt:
                            with patch.object(raster, 'get_pixmap', side_effect=AssertionError('cache miss')):
                                self.assertEqual([render.detect_lines(page, r, raster) for r in rects], expected)
                            self.assertEqual(store.detection_stats, {'hits': 2, 'misses': 0})
                        else:
                            self.assertEqual([render.detect_lines(page, r, raster) for r in rects], expected)
                    finally:
                        store.close()

    def test_failed_render_is_not_cached(self):
        with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
            page = doc.new_page()
            store = LineCache(Path(td)/'cache.json', True)
            try:
                raster = render.PageRaster(page, 'source', store)
                rect = fitz.Rect(0, 0, 100, 100)
                with patch.object(raster, 'get_pixmap', side_effect=RuntimeError('temporary')):
                    self.assertEqual(render.detect_lines(page, rect, raster), [])
                self.assertIsNone(store.detection(raster.detection_key(rect)))
                self.assertEqual(render.detect_lines(page, rect, raster), [])
                self.assertEqual(store.detection(raster.detection_key(rect)), [])
            finally:
                store.close()

    def test_invalidation_and_corrupt_boxes(self):
        with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
            page = doc.new_page()
            raster = render.PageRaster(page, 'source')
            rect = fitz.Rect(0, 0, 100, 100)
            key = raster.detection_key(rect)
            self.assertIsNone(render.PageRaster(page).detection_key(rect))
            self.assertNotEqual(key, render.PageRaster(page, 'other').detection_key(rect))
            self.assertNotEqual(key, raster.detection_key(rect+(0, 0, .01, 0)))
            for name, value in vars(render).copy().items():
                if name.isupper() and not name.startswith('_') and isinstance(value, (int, float)):
                    with patch.object(render, name, value+.01):
                        self.assertNotEqual(key, raster.detection_key(rect), name)
            with patch.object(render, '_DETECTION_CODE_SHA', 'changed'):
                self.assertNotEqual(key, raster.detection_key(rect))
            for field in ('rotation', 'cropbox', 'renderer', 'aa', 'page'):
                with patch.dict(raster.identity, {field: 'changed'}):
                    self.assertNotEqual(key, raster.detection_key(rect), field)
            store = LineCache(Path(td)/'cache.json', True)
            try:
                for value in ('broken', 'null', '[[0,0,0,0]]', '[[0,0,NaN,1]]', '[[0,1,2]]'):
                    store.db.execute('INSERT OR REPLACE INTO detections VALUES (?,?)', (key, value))
                    self.assertIsNone(store.detection(key))
            finally:
                store.close()


class BoundaryCheckpointTests(unittest.TestCase):
    def test_failed_page_retries_without_repeating_completed_page(self):
        with tempfile.TemporaryDirectory() as td:
            source, cache = Path(td)/'source.pdf', Path(td)/'boundary.json'
            with fitz.open() as doc:
                for _ in range(2):
                    doc.new_page(width=300, height=200).insert_text((40, 100), 'CHAPTER 02', fontsize=16)
                doc.save(source)
            pages = [{'width': 300, 'height': 200, 'parsing_res_list': [
                {'block_content': 'R 02', 'block_bbox': [80, 80, 145, 105]}]} for _ in range(2)]
            original = copy.deepcopy(pages)
            response = {'layoutParsingResults': [{'prunedResult': {'parsing_res_list': [
                {'block_content': 'CHAPTER 02', 'block_bbox': [0, 0, 100, 20]}]}}]}
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                ocr_boundary.repair(source, pages, cache, {'sha256': 'source'},
                    call_api=Mock(side_effect=[response, RuntimeError('interrupted')]))
            self.assertFalse(cache.exists())
            api = Mock(return_value=response)
            result = ocr_boundary.repair(source, pages, cache, {'sha256': 'source'}, call_api=api)
            self.assertEqual(api.call_count, 1)
            self.assertTrue(cache.exists())
            self.assertEqual(pages, original)
            self.assertTrue(all(p['parsing_res_list'][0]['block_content'] == 'CHAPTER 02' for p in result))

    def test_legacy_resume_identity_and_single_aggregate_export(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'boundary.json'
            identity = {'source': 'sha', 'version': 'v3', 'api': 'api'}
            legacy = {'identity': identity, 'pages': {'0': {'fingerprint': 'f0', 'result': {'old': 1}}}}
            atomic_json(path, legacy)
            cache = BoundaryCache(path, identity)
            self.assertEqual(cache.get(0, 'f0'), {'old': 1})
            cache.save_page(1, 'f1', {'new': 2})
            self.assertEqual(json.loads(path.read_text()), legacy)  # interrupted run
            resumed = BoundaryCache(path, identity)
            self.assertEqual(resumed.get(1, 'f1'), {'new': 2})
            self.assertIsNone(resumed.get(1, 'changed fingerprint'))
            self.assertIsNone(BoundaryCache(path, {**identity, 'source': 'other'}).get(1, 'f1'))
            with patch('ocr_storage.atomic_json', wraps=atomic_json) as write:
                for index in range(2, 20):
                    resumed.save_page(index, f'f{index}', {'page': index})
                resumed.finish()
                resumed.finish()
                self.assertEqual(sum(call.args[0] == path for call in write.call_args_list), 1)
            self.assertEqual(len(json.loads(path.read_text())['pages']), 20)
            (resumed.directory/'1.json').write_bytes(b'broken')
            self.assertEqual(BoundaryCache(path, identity).get(1, 'f1'), {'new': 2})


class ResponsiveServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_bounded_queue_order_and_cancellation(self):
        server = load_server(1)
        started, release = threading.Event(), threading.Event()
        calls = []
        def work(request, lang, began):
            calls.append(request.images[0])
            started.set()
            if not release.wait(5):
                raise RuntimeError('test timed out')
            return {'texts': request.images}
        with ThreadPoolExecutor(max_workers=1) as executor, \
             patch.object(server, 'request_executor', executor), \
             patch.object(server, '_recognize_request', side_effect=work):
            server.ready = True
            server.request_slots = asyncio.Semaphore(2)
            first = asyncio.create_task(server.recognize_endpoint(server.RecognitionRequest(images=['one'])))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                health = await asyncio.wait_for(server.health(), .5)
                self.assertEqual(health['status'], 'ok')
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                second = asyncio.create_task(server.recognize_endpoint(server.RecognitionRequest(images=['two'])))
                await asyncio.sleep(0)
                with self.assertRaises(server.HTTPException) as error:
                    await server.recognize_endpoint(server.RecognitionRequest(images=['three']))
                self.assertEqual(error.exception.status_code, 429)
                self.assertEqual(calls, ['one'])
                release.set()
                self.assertEqual(await asyncio.wait_for(second, 2), {'texts': ['two']})
                self.assertEqual(calls, ['one', 'two'])
                self.assertEqual(server.request_slots._value, 2)
            finally:
                release.set()

    async def test_request_timings_include_decode_and_gpu_errors_release_slot(self):
        server = load_server()
        request = server.RecognitionRequest(images=['encoded'])
        with patch.object(server, '_decode_image', return_value=np.zeros((2, 2, 3))), \
             patch.object(server, 'recognize', return_value=(['text'], [.9], {'splitSeconds': 0, 'predictSeconds': 0})):
            result = server._recognize_request(request, 'default', server.time.perf_counter())
        timing = result['timing']
        self.assertGreaterEqual(timing['requestSeconds'], timing['decodeSeconds']+timing['queueSeconds'])
        with ThreadPoolExecutor(max_workers=1) as executor, \
             patch.object(server, 'request_executor', executor), \
             patch.object(server, '_recognize_request', side_effect=ValueError('invalid image')):
            server.ready, server.request_slots = True, asyncio.Semaphore(2)
            with self.assertRaises(server.HTTPException) as error:
                await server.recognize_endpoint(request)
            self.assertEqual(error.exception.status_code, 400)
            self.assertEqual(server.request_slots._value, 2)


class BusyClientTests(unittest.TestCase):
    def test_backoff_preserves_payload_and_rejects_permanent_error(self):
        busy = ocr_api.requests.HTTPError(response=Mock(status_code=429))
        unavailable = ocr_api.requests.HTTPError(response=Mock(status_code=503))
        response = Mock()
        response.raise_for_status.side_effect = [busy, unavailable, None]
        response.json.return_value = {'texts': ['same']}
        with patch.object(ocr_api.requests, 'post', return_value=response) as post, \
             patch.object(ocr_api.time, 'sleep') as sleep:
            self.assertEqual(ocr_api.recognize_lines([b'png'], lang='korean'), ['same'])
            self.assertEqual(post.call_count, 3)
            self.assertEqual(post.call_args_list[0], post.call_args_list[2])
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])
        for status, expected_calls in ((400, 1), (429, 3), (503, 3)):
            response.raise_for_status.side_effect = ocr_api.requests.HTTPError(response=Mock(status_code=status))
            with patch.object(ocr_api.requests, 'post', return_value=response) as post, \
                 patch.object(ocr_api.time, 'sleep'), self.assertRaises(ocr_api.requests.HTTPError):
                ocr_api.recognize_lines([b'png'])
            self.assertEqual(post.call_count, expected_calls)


if __name__ == '__main__':
    unittest.main()
