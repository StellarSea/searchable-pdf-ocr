"""Production CPU pool: exact output, cache-first resume, budgets and recovery."""
from contextlib import redirect_stdout
import copy
import hashlib
import io
import inspect
import multiprocessing as mp
from pathlib import Path
import sys
import tempfile
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_to_searchable_pdf as ocr
from ocr_prepare import CPUPreparer, PixelsRequired
from ocr_storage import LineCache
from test_ocr_prefetch import fixture


def quality(report):
    return {key: value for key, value in report.items() if not key.endswith('_stats')}


class CPUPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.layouts, _ = fixture(self.root)
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.font_spec = {'fontname': 'helv'}

    def repeat(self, count):
        repeated = self.root/'repeated.pdf'
        with fitz.open(self.source) as source, fitz.open() as doc:
            for _ in range(count):
                doc.insert_pdf(source)
            doc.save(repeated)
        self.source = repeated
        self.layouts *= count
        self.sha = hashlib.sha256(repeated.read_bytes()).hexdigest()

    def overlay(self, name, workers, recognize=None, cache_name=None, **kwargs):
        cache = self.root/f'{cache_name or name}.json'
        refiner = ocr.LineRefiner(cache, set(range(1, len(self.layouts)+1)),
                                 automatic=True, source_sha256=self.sha)
        try:
            with redirect_stdout(io.StringIO()), patch.object(ocr, 'recognize_lines',
                    side_effect=recognize or (lambda images, **kw: ['?']*len(images))):
                report = ocr.overlay(self.source, self.layouts, self.root/f'{name}.pdf',
                                     line_refiner=refiner, automatic=True, cpu_workers=workers, **kwargs)
            return report, copy.deepcopy(refiner.cache['decisions'])
        finally:
            refiner.close()

    def assert_output_equal(self, a, b):
        with fitz.open(self.root/f'{a}.pdf') as before, fitz.open(self.root/f'{b}.pdf') as after:
            self.assertEqual(len(before), len(after))
            self.assertEqual(before.get_toc(), after.get_toc())
            for p, q in zip(before, after):
                self.assertEqual(p.get_text('rawdict'), q.get_text('rawdict'))
                self.assertEqual(p.get_pixmap().samples, q.get_pixmap().samples)

    def test_long_document_default_pool_matches_serial_and_has_bounded_window(self):
        self.repeat(72)  # Exercises worker recycling, not just the first window.
        before, decisions = self.overlay('serial', 0)
        after, selected = self.overlay('parallel', 4, cpu_memory_mb=4096)
        self.assertEqual(quality(before), quality(after))
        self.assertEqual(decisions, selected)
        stats = after['cpu_prepare_stats']
        self.assertEqual(stats['prepared_pages'], 72)
        self.assertEqual(stats['fallbacks'], [])
        self.assertLessEqual(stats['peak_pending_pages'], 4)
        self.assertLessEqual(stats['peak_reserved_mib'], 4096)
        self.assert_output_equal('serial', 'parallel')

    def test_warm_cache_never_spawns_or_renders_or_calls_http(self):
        before, decisions = self.overlay('cold', 4, cache_name='shared')
        with patch('ocr_render.PageRaster.get_pixmap', side_effect=AssertionError('warm render')):
            after, selected = self.overlay('warm', 4, cache_name='shared',
                                           recognize=Mock(side_effect=AssertionError('warm HTTP')))
        self.assertEqual(quality(before), quality(after))
        self.assertEqual(decisions, selected)
        self.assertEqual(after['cpu_prepare_stats']['pool_starts'], 0)
        self.assertEqual(after['cpu_prepare_stats']['prepared_pages'], 0)
        self.assertEqual(after['cpu_prepare_stats']['cached_pages'], 1)
        self.assert_output_equal('cold', 'warm')

    def test_partial_http_failure_resumes_with_old_responses_and_same_output(self):
        before, decisions = self.overlay('reference', 0)
        calls = []
        def fail_after_one(images, **kwargs):
            calls.append(len(images))
            if len(calls) > 1:
                raise RuntimeError('injected HTTP interruption')
            return ['?']*len(images)
        with self.assertRaisesRegex(RuntimeError, 'injected HTTP interruption'):
            self.overlay('interrupted', 4, recognize=fail_after_one, cache_name='resume')
        resumed_calls = []
        def recognize(images, **kwargs):
            resumed_calls.extend(images)
            return ['?']*len(images)
        after, selected = self.overlay('resumed', 4, recognize=recognize, cache_name='resume')
        self.assertEqual(quality(before), quality(after))
        self.assertEqual(decisions, selected)
        self.assertEqual(len(resumed_calls), 4)  # First two line responses survive.
        self.assert_output_equal('reference', 'resumed')

    def test_memory_pressure_falls_back_without_changing_output(self):
        before, decisions = self.overlay('reference', 0)
        with patch.object(CPUPreparer, 'pressure', return_value=True):
            after, selected = self.overlay('pressure', 4)
        self.assertEqual(quality(before), quality(after))
        self.assertEqual(decisions, selected)
        self.assertEqual(after['cpu_prepare_stats']['pool_starts'], 0)
        self.assertEqual(len(after['cpu_prepare_stats']['fallbacks']), 1)
        self.assert_output_equal('reference', 'pressure')

    def test_debug_and_toc_match_serial(self):
        self.repeat(3)
        kwargs = {'debug': True, 'toc': [[1, 'First', 1], [1, 'Last', 3]]}
        before, decisions = self.overlay('debug-before', 0, **kwargs)
        after, selected = self.overlay('debug-after', 4, **kwargs)
        self.assertEqual(quality(before), quality(after))
        self.assertEqual(decisions, selected)
        self.assert_output_equal('debug-before', 'debug-after')

    def test_cli_defaults_optout_and_completion_identity(self):
        default = ocr.build_parser().parse_args(['source.pdf'])
        serial = ocr.build_parser().parse_args(['source.pdf', '--cpu-workers', '0', '--cpu-memory-mb', '512'])
        self.assertEqual(default.cpu_workers, 16)
        self.assertEqual(inspect.signature(CPUPreparer).parameters['workers'].default, 16)
        self.assertEqual(default.cpu_memory_mb, 16384)
        self.assertEqual(inspect.signature(CPUPreparer).parameters['memory_mb'].default, 16384)
        self.assertEqual(inspect.signature(ocr.overlay).parameters['cpu_memory_mb'].default, 16384)
        self.assertEqual(ocr.run_options(default), ocr.run_options(serial))
        for arguments in (['--cpu-workers', '-1'], ['--cpu-workers', '17'], ['--cpu-memory-mb', '0']):
            with self.assertRaises(SystemExit):
                ocr.build_parser().parse_args(['source.pdf', *arguments])

    def test_sixteen_workers_match_serial(self):
        self.repeat(17)
        before, decisions = self.overlay('sixteen-reference', 0)
        with patch('ocr_prepare.psutil.cpu_count', return_value=16):
            after, selected = self.overlay('sixteen', 16)
        self.assertEqual(quality(before), quality(after))
        self.assertEqual(decisions, selected)
        self.assertEqual(after['cpu_prepare_stats']['fallbacks'], [])
        self.assertEqual(after['cpu_prepare_stats']['workers'], 16)
        self.assertLessEqual(after['cpu_prepare_stats']['peak_pending_pages'], 16)
        self.assertLessEqual(after['cpu_prepare_stats']['peak_buffered_pages'], 32)
        self.assert_output_equal('sixteen-reference', 'sixteen')

    def test_http_wait_pumps_only_on_owner_thread(self):
        owner = threading.get_ident()
        waiting, pumped = threading.Event(), threading.Event()
        observed = []
        original_pump = CPUPreparer.pump
        def pump(prep, *args, **kwargs):
            self.assertEqual(threading.get_ident(), owner)
            if waiting.is_set():
                pumped.set()
            return original_pump(prep, *args, **kwargs)
        def recognize(images, **kwargs):
            if not observed:
                waiting.set()
                observed.append(pumped.wait(3))
            return ['?']*len(images)
        with patch.object(CPUPreparer, 'pump', pump):
            report, _ = self.overlay('idle-pump', 1, recognize=recognize)
        self.assertEqual(observed, [True])
        self.assertEqual(report['cpu_prepare_stats']['fallbacks'], [])

    def test_logical_cpu_limit_allows_sixteen_on_eight_core_cpu(self):
        with patch('ocr_prepare.psutil.cpu_count', return_value=16) as count:
            with CPUPreparer(self.source, self.layouts*20, Mock(), self.sha,
                             self.font_spec, {1}) as prep:
                self.assertEqual(prep.workers, 16)
                count.assert_called_once_with(logical=True)

    def test_startup_does_not_spawn_sixteen_for_two_admissible_pages(self):
        mib = 1024**2
        with CPUPreparer(self.source, self.layouts*20, Mock(), self.sha, self.font_spec,
                         {1}, workers=16, memory_probe=lambda: 4096*mib) as prep:
            prep.plan = Mock(side_effect=lambda index: (
                {'page': index, 'reservation': 512*mib, 'key': str(index)}, None))
            context = Mock()
            with patch('ocr_prepare.mp.get_context', return_value=context):
                prep.fill(0, required=0)
            self.assertEqual(context.Pool.call_args.args[0], 2)
            self.assertEqual(prep.pool_workers, 2)
            self.assertEqual(prep.stats['pool_workers'], 2)
            self.assertTrue(prep.stats['memory_limited_pool'])
            self.assertEqual(len(prep.pending), 2)
            self.assertEqual(prep.stats['fallbacks'], [])
            self.assertLessEqual(prep.reserved(), 1024*mib)

    def test_startup_keeps_full_pool_when_memory_allows_it(self):
        mib = 1024**2
        with patch('ocr_prepare.psutil.cpu_count', return_value=16), \
             CPUPreparer(self.source, self.layouts*20, Mock(), self.sha, self.font_spec,
                         {1}, workers=16, memory_probe=lambda: 32*1024*mib) as prep:
            prep.plan = Mock(side_effect=lambda index: (
                {'page': index, 'reservation': 512*mib, 'key': str(index)}, None))
            context = Mock()
            with patch('ocr_prepare.mp.get_context', return_value=context):
                prep.fill(0, required=0)
            self.assertEqual(context.Pool.call_args.args[0], 16)
            self.assertFalse(prep.stats['memory_limited_pool'])
            self.assertEqual(len(prep.pending), 16)

    def test_slow_first_page_refills_before_consumption_and_publishes_in_order(self):
        store = Mock()
        with CPUPreparer(self.source, self.layouts*12, store, self.sha, self.font_spec,
                         {1}, workers=2, timeout=1, memory_probe=lambda: 32*1024**3) as prep:
            prep.pressure = Mock(return_value=False)
            prep.plan = Mock(side_effect=lambda index: (
                {'page': index, 'reservation': 300*1024**2, 'key': str(index)}, None))
            submitted, results = [], {}
            def value(index):
                return {'page': index, 'key': str(index), 'blocks': {},
                        'detections': {str(index): []}, 'png_bytes': 0}
            def submit(function, args):
                index = args[0]['page']
                submitted.append(index)
                result = results[index] = Mock()
                result.ready.return_value = index == 1
                result.get.return_value = value(index)
                if index == 0:
                    def slow_get(**kwargs):
                        if len(submitted) == 2:
                            raise mp.TimeoutError()
                        self.assertIn(2, submitted, 'Idle slot was not refilled while p0 waited')
                        store.save_detection.assert_not_called()
                        return value(0)
                    result.get.side_effect = slow_get
                return result
            prep.pool = Mock()
            prep.pool.apply_async.side_effect = submit
            self.assertEqual(prep(0), {})
            self.assertGreater(prep.stats['refill_jobs'], 0)
            store.save_detection.assert_called_once_with('0', [])
            self.assertEqual(prep(1), {})
            self.assertEqual([call.args[0] for call in store.save_detection.call_args_list], ['0', '1'])
            self.assertLessEqual(prep.stats['peak_pending_pages'], 2)
            self.assertLessEqual(prep.stats['peak_buffered_pages'], 4)

    def test_future_memory_deferral_retries_after_slot_release(self):
        with CPUPreparer(self.source, self.layouts*8, Mock(), self.sha, self.font_spec,
                         {1}, workers=2, memory_mb=512, memory_probe=lambda: 32*1024**3) as prep:
            prep.plan = Mock(side_effect=lambda index: (
                {'page': index, 'reservation': 300*1024**2, 'key': str(index)}, None))
            prep.pool = Mock()
            prep.fill(0)
            self.assertEqual(list(prep.pending), [0])
            self.assertNotIn(1, prep.ready)
            self.assertIn(1, prep.plans)
            prep.fill(1)  # HTTP pump's next-page floor is not yet demanded.
            self.assertNotIn(1, prep.ready)
            prep.accept(0, '0', {'page': 0, 'key': '0', 'blocks': {},
                               'detections': {}, 'png_bytes': 0})
            prep.fill(0)
            self.assertIn(1, prep.pending)
            self.assertEqual(prep.stats['memory_skips'], 0)
            self.assertLessEqual(prep.stats['peak_reserved_mib'], 512)

    def test_legacy_and_paragraph_do_not_spawn(self):
        with patch.object(ocr, 'CPUPreparer', side_effect=AssertionError('legacy pool')):
            with redirect_stdout(io.StringIO()):
                ocr.overlay(self.source, self.layouts, self.root/'legacy.pdf', cpu_workers=4)
                ocr.overlay(self.source, self.layouts, self.root/'paragraph.pdf',
                            paragraph_mode=True, automatic=True, cpu_workers=4)

    def test_worker_failure_timeout_and_bad_identity_are_sticky_serial_fallback(self):
        for failure in ('exception', 'timeout', 'identity'):
            with self.subTest(failure=failure):
                store = LineCache(self.root/f'{failure}.json', automatic=True)
                try:
                    with CPUPreparer(self.source, self.layouts, store, self.sha, self.font_spec,
                                     {1}, workers=1, timeout=.001, memory_probe=lambda: 32*1024**3) as prep:
                        pool = prep.pool = Mock()
                        result = pool.apply_async.return_value
                        result.ready.return_value = False
                        if failure == 'exception':
                            result.get.side_effect = RuntimeError('worker died')
                        elif failure == 'timeout':
                            result.get.side_effect = mp.TimeoutError()
                        else:
                            result.get.return_value = {'page': 99, 'key': 'wrong'}
                        with redirect_stdout(io.StringIO()):
                            self.assertIsNone(prep(0))
                        self.assertTrue(prep.disabled)
                        self.assertIsNone(prep(0))
                        self.assertEqual(len(prep.stats['fallbacks']), 1)
                        pool.terminate.assert_called_once()
                        pool.join.assert_called_once()
                finally:
                    store.close()

    def test_budget_admission_skips_without_spawning(self):
        store = LineCache(self.root/'budget.json', automatic=True)
        try:
            with CPUPreparer(self.source, self.layouts, store, self.sha, self.font_spec,
                             {1}, workers=4, memory_mb=256, memory_probe=lambda: 32*1024**3) as prep:
                self.assertIsNone(prep(0))
                self.assertEqual(prep.stats['pool_starts'], 0)
                self.assertGreater(prep.stats['memory_skips'], 0)
        finally:
            store.close()

    def test_rotated_cropped_page_and_no_http_prefetch_match(self):
        rotated = self.root/'rotated.pdf'
        with fitz.open(self.source) as doc:
            doc[0].set_cropbox(fitz.Rect(5, 5, 245, 275))
            doc[0].set_rotation(90)
            doc.save(rotated)
        self.source = rotated
        self.sha = hashlib.sha256(rotated.read_bytes()).hexdigest()
        owner = threading.get_ident()
        def recognize(images, **kwargs):
            self.assertEqual(threading.get_ident(), owner)
            return ['?']*len(images)
        before, decisions = self.overlay('rotated-before', 0, debug=True, prefetch=False, recognize=recognize)
        after, selected = self.overlay('rotated-after', 4, debug=True, prefetch=False, recognize=recognize)
        self.assertEqual(quality(before), quality(after))
        self.assertEqual(decisions, selected)
        self.assert_output_equal('rotated-before', 'rotated-after')

    def test_missing_crop_index_rebuilds_only_missing_png_without_http(self):
        before, decisions = self.overlay('before-index-loss', 4, cache_name='index')
        db = sqlite3.connect(self.root/'index.sqlite3')
        try:
            db.execute('DELETE FROM crop_index WHERE key=(SELECT key FROM crop_index LIMIT 1)')
            db.commit()
        finally:
            db.close()
        after, selected = self.overlay('after-index-loss', 4, cache_name='index',
                                       recognize=Mock(side_effect=AssertionError('cached image called HTTP')))
        self.assertEqual(after['line_cache_stats']['rendered_crops'], 1)
        self.assertEqual(after['cpu_prepare_stats']['prepared_pages'], 1)
        self.assertEqual(quality(before), quality(after))
        self.assertEqual(decisions, selected)
        self.assert_output_equal('before-index-loss', 'after-index-loss')

    def test_keyboard_interrupt_terminates_only_owned_pool(self):
        store = LineCache(self.root/'interrupt.json', automatic=True)
        pool = Mock()
        try:
            with self.assertRaises(KeyboardInterrupt):
                with CPUPreparer(self.source, self.layouts, store, self.sha, self.font_spec, {1}) as prep:
                    prep.pool = pool
                    raise KeyboardInterrupt()
            pool.terminate.assert_called_once()
            pool.join.assert_called_once()
        finally:
            store.close()

    def test_cached_table_pixel_probe_only_bypasses_that_page(self):
        before, decisions = self.overlay('table-reference', 0)
        with patch('ocr_prepare.block_layout', side_effect=PixelsRequired('table columns need pixels')):
            after, selected = self.overlay('table-bypass', 4)
        stats = after['cpu_prepare_stats']
        self.assertEqual(stats['pool_starts'], 0)
        self.assertEqual(stats['serial_layout_pages'], 1)
        self.assertEqual(stats['fallbacks'], [])
        self.assertEqual(quality(before), quality(after))
        self.assertEqual(decisions, selected)
        self.assert_output_equal('table-reference', 'table-bypass')
