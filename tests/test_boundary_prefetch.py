"""Owner-only boundary preparation/checkpoints with one in-flight HTTP request."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_boundary as boundary
from ocr_artifacts import TextArtifact
from ocr_storage import BoundaryCache


def response(text='CHAPTER 02'):
    return {'layoutParsingResults': [{'prunedResult': {'parsing_res_list': [
        {'block_content': text, 'block_bbox': [0, 0, 100, 20]}]}}]}


class BoundaryPrefetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.pdf'
        self.owner = threading.get_ident()
        self.pages = []
        with fitz.open() as doc:
            for _ in range(3):
                page = doc.new_page(width=300, height=200)
                page.insert_text((40, 100), 'CHAPTER 02', fontsize=16)
                self.pages.append({'width': 300, 'height': 200, 'parsing_res_list': [
                    {'block_content': 'R 02', 'block_bbox': [80, 80, 145, 105],
                     'block_polygon_points': [[80, 80], [145, 105]]}]})
            doc.save(self.source)
        self.identity = {'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest()}

    def cache(self, name):
        return TextArtifact(self.root / (name + '.sqlite3'), 'boundary.json')

    def saved(self, path):
        return BoundaryCache(path, {'source': self.identity['sha256'],
            'version': boundary.VERSION, 'api': boundary.ocr_api.API})

    def run_repair(self, path, api, **kwargs):
        return boundary.repair(self.source, self.pages, path, self.identity,
                               call_api=api, **kwargs)

    def test_serial_and_prefetch_exact_inputs_decisions_checkpoints_and_owner(self):
        # Include rejected recovery, Unicode, a cropped/rotated annotated page,
        # and multiple requests on one page. No PDF operation enters HTTP.
        with fitz.open(self.source) as doc:
            doc[0].insert_text((40, 160), 'CHAPTER 02', fontsize=16)
            doc[2].set_cropbox(fitz.Rect(10, 10, 290, 190))
            doc[2].set_rotation(90)
            doc[2].add_rect_annot(fitz.Rect(30, 80, 160, 110)).update()
            doc.save(self.root / 'variant.pdf')
        self.source = self.root / 'variant.pdf'
        self.identity = {'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest()}
        self.pages[0]['parsing_res_list'].append(
            {'block_content': 'R 02', 'block_bbox': [80, 140, 145, 165]})
        original, source_bytes = copy.deepcopy(self.pages), self.source.read_bytes()
        outcomes = []
        for overlap in (False, True):
            path, pngs, commits, stats = self.cache(str(overlap)), [], [], {}
            save_page = BoundaryCache.save_page
            get_pixmap = fitz.Page.get_pixmap

            def save(cache, index, fingerprint, value):
                self.assertEqual(threading.get_ident(), self.owner)
                commits.append(index)
                return save_page(cache, index, fingerprint, value)

            def raster(page, *args, **kwargs):
                self.assertEqual(threading.get_ident(), self.owner)
                return get_pixmap(page, *args, **kwargs)

            def api(png, file_type, **options):
                self.assertEqual(threading.get_ident() == self.owner, not overlap)
                self.assertEqual(file_type, 1)
                self.assertEqual(options, {'options': {'useLayoutDetection': False}})
                pngs.append(png)
                return response('변경 99' if len(pngs) == 2 else 'CHAPTER 02 한글')

            class Service:
                def ensure(service):
                    self.assertEqual(threading.get_ident(), self.owner)

            with patch.object(BoundaryCache, 'save_page', save), patch.object(fitz.Page, 'get_pixmap', raster):
                result = self.run_repair(path, api, prefetch=overlap, stats=stats, service=Service())
            self.assertGreaterEqual(len(pngs), 3)
            self.assertEqual(commits, [0, 1, 2])
            self.assertEqual(stats['requests'], len(pngs))
            self.assertEqual(stats['errors'], 0)
            outcomes.append((result, pngs, json.loads(path.read_text())))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(self.pages, original)
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertFalse(outcomes[0][0][0]['parsing_res_list'][1]['boundary_audit']['accepted'])

    def test_next_page_preparation_overlaps_but_commit_precedes_next_request(self):
        started, prepared = threading.Event(), threading.Event()
        cache = self.cache('overlap')
        requests, commits = [], []
        get_pixmap, save_page = fitz.Page.get_pixmap, BoundaryCache.save_page

        def raster(page, *args, **kwargs):
            if page.number == 1:
                self.assertTrue(started.wait(5), 'HTTP did not start')
                prepared.set()
            return get_pixmap(page, *args, **kwargs)

        def save(store, index, fingerprint, value):
            self.assertEqual(threading.get_ident(), self.owner)
            commits.append(index)
            return save_page(store, index, fingerprint, value)

        def api(*args, **kwargs):
            index = len(requests)
            self.assertEqual(commits, list(range(index)))
            requests.append(index)
            if index == 0:
                started.set()
                self.assertTrue(prepared.wait(5), 'Next page was not prepared during HTTP')
            return response()

        with patch.object(fitz.Page, 'get_pixmap', raster), patch.object(BoundaryCache, 'save_page', save):
            self.run_repair(cache, api, prefetch=True)
        self.assertEqual(requests, [0, 1, 2])
        self.assertEqual(commits, [0, 1, 2])

    def test_no_request_middle_page_and_warm_cache(self):
        self.pages[1]['parsing_res_list'][0]['block_label'] = 'reviewed_line'
        first_prepared = threading.Event()
        get_pixmap = fitz.Page.get_pixmap
        calls = []
        cache = self.cache('middle')

        def raster(page, *args, **kwargs):
            if page.number == 1:
                first_prepared.set()
            return get_pixmap(page, *args, **kwargs)

        def api(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                self.assertTrue(first_prepared.wait(5))
            return response()

        with patch.object(fitz.Page, 'get_pixmap', raster):
            result = self.run_repair(cache, api, prefetch=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(json.loads(cache.read_text())['pages']), ['0', '1', '2'])
        with patch.object(fitz.Page, 'get_pixmap', side_effect=AssertionError('warm cache raster')):
            self.assertEqual(self.run_repair(cache, lambda *a, **k: self.fail('warm cache HTTP'),
                                             prefetch=True), result)

    def test_http_failure_preserves_prior_page_and_resume_only_unfinished(self):
        for overlap in (False, True):
            cache, calls, stats = self.cache('failure' + str(overlap)), [], {}

            def api(*args, **kwargs):
                calls.append(1)
                if len(calls) == 2:
                    raise RuntimeError('HTTP failed')
                return response()

            with self.assertRaisesRegex(RuntimeError, 'HTTP failed'):
                self.run_repair(cache, api, prefetch=overlap, stats=stats)
            store = self.saved(cache)
            self.assertTrue(store.page_entry(0).exists())
            self.assertFalse(store.page_entry(1).exists())
            self.assertFalse(cache.exists())
            self.assertEqual(stats['errors'], 1)
            resumed = []
            result = self.run_repair(cache, lambda *a, **k: (resumed.append(1), response())[1], prefetch=overlap)
            self.assertEqual(len(resumed), 2)
            self.assertEqual(len(result), 3)

    def test_later_page_preparation_failure_finishes_prior_checkpoint(self):
        for error in (ValueError('render failed'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                cache = self.cache(type(error).__name__)
                failed = threading.Event()
                get_pixmap = fitz.Page.get_pixmap

                def raster(page, *args, **kwargs):
                    if page.number == 1:
                        failed.set()
                        raise error
                    return get_pixmap(page, *args, **kwargs)

                def api(*args, **kwargs):
                    self.assertTrue(failed.wait(5))
                    return response()

                with patch.object(fitz.Page, 'get_pixmap', raster), self.assertRaises(type(error)):
                    self.run_repair(cache, api, prefetch=True)
                self.assertTrue(self.saved(cache).page_entry(0).exists())
                self.assertFalse(self.saved(cache).page_entry(1).exists())
                self.assertFalse(cache.exists())

    def test_same_page_preparation_failure_does_not_checkpoint_partial_page(self):
        self.pages[0]['parsing_res_list'].append(
            {'block_content': 'R 02', 'block_bbox': [80, 140, 145, 165]})
        failed = threading.Event()
        expanded_box, count = boundary.expanded_box, []
        cache = self.cache('partial')

        def detect(*args, **kwargs):
            count.append(1)
            if len(count) == 2:
                failed.set()
                raise RuntimeError('next block failed')
            return expanded_box(*args, **kwargs)

        def api(*args, **kwargs):
            self.assertTrue(failed.wait(5))
            return response()

        with patch.object(boundary, 'expanded_box', detect), self.assertRaisesRegex(RuntimeError, 'next block'):
            self.run_repair(cache, api, prefetch=True)
        self.assertFalse(self.saved(cache).page_entry(0).exists())

    def test_checkpoint_failure_is_not_retried_and_stops_next_http(self):
        cache, calls, commits, stats = self.cache('write-failure'), [], [], {}

        def api(*args, **kwargs):
            calls.append(1)
            return response()

        def save(store, *args):
            self.assertEqual(threading.get_ident(), self.owner)
            commits.append(1)
            raise OSError('checkpoint write failed')

        with patch.object(BoundaryCache, 'save_page', save), self.assertRaisesRegex(OSError, 'checkpoint write'):
            self.run_repair(cache, api, prefetch=True, stats=stats)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(commits), 1)
        self.assertEqual(stats['errors'], 0)
        self.assertFalse(cache.exists())

    def test_cached_middle_page_keeps_prior_and_next_checkpoint_order(self):
        cache = self.cache('cached-middle')
        fingerprint = hashlib.sha256(json.dumps(self.pages[1], sort_keys=True,
                                               ensure_ascii=False).encode()).hexdigest()
        store = self.saved(cache)
        store.save_page(1, fingerprint, self.pages[1])
        requests, commits = [], []
        prepared = threading.Event()
        get_pixmap, save_page = fitz.Page.get_pixmap, BoundaryCache.save_page

        def raster(page, *args, **kwargs):
            self.assertNotEqual(page.number, 1, 'Cached page was rendered again')
            if page.number == 2:
                prepared.set()
            return get_pixmap(page, *args, **kwargs)

        def save(cache, index, *args):
            commits.append(index)
            return save_page(cache, index, *args)

        def api(*args, **kwargs):
            requests.append(1)
            if len(requests) == 1:
                self.assertTrue(prepared.wait(5))
            else:
                self.assertEqual(commits, [0])
            return response()

        with patch.object(fitz.Page, 'get_pixmap', raster), patch.object(BoundaryCache, 'save_page', save):
            result = self.run_repair(cache, api, prefetch=True)
        self.assertEqual(commits, [0, 2])
        self.assertEqual(len(requests), 2)
        self.assertEqual(result[1], self.pages[1])

    def test_earlier_http_error_precedes_later_preparation_error(self):
        cache, failed = self.cache('error-order'), threading.Event()
        get_pixmap = fitz.Page.get_pixmap

        def raster(page, *args, **kwargs):
            if page.number == 1:
                failed.set()
                raise ValueError('Later preparation error')
            return get_pixmap(page, *args, **kwargs)

        def api(*args, **kwargs):
            self.assertTrue(failed.wait(5))
            raise RuntimeError('Earlier HTTP error')

        with patch.object(fitz.Page, 'get_pixmap', raster), self.assertRaisesRegex(RuntimeError, 'Earlier HTTP'):
            self.run_repair(cache, api, prefetch=True)
        self.assertFalse(self.saved(cache).page_entry(0).exists())
        self.assertFalse(cache.exists())


if __name__ == '__main__':
    unittest.main()
