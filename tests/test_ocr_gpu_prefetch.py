"""GPU feed keeps identical crops, owner-only persistence and bounded work."""
from concurrent.futures import Future, ThreadPoolExecutor
import copy
import hashlib
import io
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
from ocr_gpu_prefetch import GPUPrefetch
from ocr_storage import LineCache
from ocr_layout import line_settings
import ocr_to_searchable_pdf as ocr
from test_ocr_prefetch import fixture


class GPUFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.layouts, _ = fixture(self.root)
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def run_overlay(self, name, enabled, cache=None, recognize=None):
        refiner = ocr.LineRefiner(self.root/f'{cache or name}.json', {1}, automatic=True,
                                 source_sha256=self.sha)
        calls = []
        def api(images, **kwargs):
            calls.append((kwargs.get('lang'), [hashlib.sha256(png).hexdigest() for png in images]))
            return (recognize(images, **kwargs) if recognize else
                    [hashlib.sha256(png).hexdigest()[:12] for png in images])
        try:
            with patch.object(ocr, 'recognize_lines', api), redirect_stdout(io.StringIO()):
                report = ocr.overlay(self.source, self.layouts, self.root/f'{name}.pdf',
                    line_refiner=refiner, automatic=True, cpu_workers=1, gpu_prefetch=enabled)
            return report, copy.deepcopy(refiner.cache['decisions']), calls
        finally:
            refiner.close()

    def test_cross_block_batch_preserves_pixels_text_boxes_keys_and_decisions(self):
        before, old, old_calls = self.run_overlay('before', False)
        after, new, calls = self.run_overlay('after', True)
        comparable = lambda report: {k: v for k, v in report.items() if not k.endswith('_stats')}
        self.assertEqual(comparable(before), comparable(after))
        self.assertEqual(old, new)
        self.assertEqual(len(old_calls), 3)
        self.assertEqual(len(calls), 1)
        self.assertEqual([h for _, hashes in old_calls for h in hashes], calls[0][1])
        self.assertEqual(after['gpu_feed_stats']['errors'], [])
        with fitz.open(self.root/'before.pdf') as a, fitz.open(self.root/'after.pdf') as b:
            self.assertEqual(a[0].get_text('rawdict'), b[0].get_text('rawdict'))
            self.assertEqual(a[0].get_pixmap().samples, b[0].get_pixmap().samples)

    def test_warm_cache_uses_no_gpu_requests(self):
        self.run_overlay('cold', True, cache='shared')
        report, _, calls = self.run_overlay('warm', True, cache='shared',
                            recognize=Mock(side_effect=AssertionError('warm HTTP')))
        self.assertEqual(calls, [])
        self.assertEqual(report['gpu_feed_stats']['requests'], 0)
        self.assertEqual(report['cpu_prepare_stats']['pool_starts'], 0)

    def test_speculation_failure_uses_original_per_block_path(self):
        expected, decisions, _ = self.run_overlay('reference', False)
        def api(images, **kwargs):
            if len(images) > 2:
                raise RuntimeError('injected speculative failure')
            return [hashlib.sha256(png).hexdigest()[:12] for png in images]
        report, actual, calls = self.run_overlay('failed-feed', True, recognize=api)
        self.assertEqual(actual, decisions)
        self.assertEqual(len(report['gpu_feed_stats']['errors']), 1)
        self.assertEqual(len(calls), 4)

    def test_limits_languages_result_validation_and_owner(self):
        store = LineCache(self.root/'limits.json', automatic=True)
        self.addCleanup(store.close)
        executor = Mock()
        futures = []
        def submit(*args, **kwargs):
            future = Future()
            futures.append(future)
            return future
        executor.submit.side_effect = submit
        blocks = {0: ([[], []], [], {(0,): b'a', (1,): b'b'}),
                  1: ([[], []], [], {(2,): b'c', (3,): b'd'}),
                  2: ([[], []], [], {(4,): b'e', (5,): b'f'})}
        self.layouts[0]['parsing_res_list'][1]['block_content'] = '한글 본문'
        with GPUPrefetch(store, executor, Mock(), self.layouts, {1},
                         max_images=2, max_requests=2, budget=32) as feed:
            feed.offer([(0, blocks)])
            self.assertEqual(len(feed.jobs), 2)
            self.assertEqual([call.kwargs['lang'] for call in executor.submit.call_args_list], [None, 'korean'])
            self.assertLessEqual(feed.stats['peak_buffer_bytes'], 32)
            self.assertEqual(store.data['decisions'], [])
            futures[0].set_result(['A', 'B'])
            feed.offer([(0, blocks)])
            self.assertEqual(len(feed.jobs), 2)  # Finished slot refilled.
            self.assertEqual(len(futures), 3)
            with ThreadPoolExecutor(1) as other:
                with self.assertRaisesRegex(RuntimeError, 'owner thread'):
                    other.submit(feed.harvest).result()
            futures[1].set_result(['wrong length'])
            with redirect_stdout(io.StringIO()):
                feed.harvest()
            self.assertTrue(feed.disabled)
            self.assertTrue(futures[2].cancelled())
            key = hashlib.sha256(line_settings('First line Second line').encode()+b'a').hexdigest()
            self.assertEqual(store.response(key), 'A')  # Earlier valid batch survives.

    def test_optout_does_not_change_completion_identity(self):
        default = ocr.build_parser().parse_args(['book.pdf'])
        off = ocr.build_parser().parse_args(['book.pdf', '--no-gpu-prefetch'])
        self.assertFalse(default.no_gpu_prefetch)
        self.assertEqual(ocr.run_options(default), ocr.run_options(off))
