"""Exact serial/parallel findings and fail-closed process fallback, without OCR."""
from collections import Counter
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_verify as verification
import ocr_workflow
import ocr_modes


class VerificationTests(unittest.TestCase):
    def test_cli_default_optout_and_completion_identity(self):
        parser = ocr_modes.build_parser()
        default = parser.parse_args(['book.pdf'])
        serial = parser.parse_args(['book.pdf', '--verify-workers', '0'])
        self.assertEqual(default.verify_workers, 8)
        self.assertEqual(default.cpu_workers, 16)
        self.assertEqual(serial.verify_workers, 0)
        self.assertEqual(ocr_modes.run_options(default), ocr_modes.run_options(serial))

    def fixture(self, root):
        source, output = root/'source.pdf', root/'output.pdf'
        with fitz.open() as doc:
            for rotation in (0, 90, 180, 270, 0, 0):
                page = doc.new_page(width=240, height=190)
                page.set_cropbox(fitz.Rect(5, 7, 230, 180))
                page.set_rotation(rotation)
            doc[3].insert_text((25, 40), 'Existing')
            doc.save(source)
        with fitz.open(source) as doc:
            doc[0].insert_text((25, 40), 'Hello', render_mode=3)
            doc[1].insert_text((25, 40), 'Visible')
            doc[2].insert_text((25, 40), 'Missing', render_mode=3)
            doc[4].draw_rect(fitz.Rect(10, 10, 40, 40), fill=(1, 0, 0))
            doc[5].insert_text((25, 40), 'Extra X', render_mode=3)
            doc.save(output)
        texts = ['Hello', 'Visible', 'Missing X', 'Existing', '', 'Extra']
        report = {'pages': [{'page': i+1, 'warnings': ['existing_text_preserved'] if i == 3 else [],
                            'expected_characters': Counter(c for c in text if not c.isspace()),
                            'existing_text': i == 3} for i, text in enumerate(texts)]}
        return source, output, report

    def test_parallel_preserves_findings_order_and_report_object_identities(self):
        with tempfile.TemporaryDirectory() as td:
            source, output, report = self.fixture(Path(td))
            serial = verification.verify(source, output, copy.deepcopy(report))
            self.assertEqual(serial['pages'][0]['warnings'], [])
            self.assertEqual(serial['pages'][1]['warnings'], ['visible_page_changed', 'non_invisible_text'])
            self.assertEqual(serial['pages'][2]['missing'], {'X': 1})
            self.assertEqual(serial['pages'][4]['warnings'], ['visible_page_changed'])
            self.assertEqual(serial['pages'][5]['extra'], {'X': 1})
            self.assertTrue(serial['validation_failed'])
            pages, page_refs, stats = report['pages'], list(report['pages']), {}
            with patch.object(verification, 'MIN_PARALLEL_PIXELS', 0):
                checked = verification.verify(source, output, report, workers=2, stats=stats)
            self.assertEqual(checked, serial)
            self.assertIs(checked, report)
            self.assertIs(report['pages'], pages)
            self.assertTrue(all(a is b for a, b in zip(report['pages'], page_refs)))
            self.assertEqual(stats['workers'], 2)
            self.assertEqual(stats['pool_pages'], 6)
            self.assertEqual(stats['serial_pages'], 0)
            self.assertLessEqual(stats['peak_pending'], 2)
            self.assertIsNone(stats['fallback'])

    def test_debug_still_checks_text_and_visibility_but_skips_pixel_pool(self):
        with tempfile.TemporaryDirectory() as td:
            source, output, report = self.fixture(Path(td))
            with patch.object(verification.mp, 'get_context', side_effect=AssertionError('spawned debug pool')):
                verification.verify(source, output, report, debug=True, workers=16)
            self.assertEqual(report['pages'][1]['warnings'], ['non_invisible_text'])
            self.assertEqual(report['pages'][4]['warnings'], [])
            self.assertTrue(report['validation_failed'])

    def test_memory_and_small_document_admission_use_complete_serial_check(self):
        with tempfile.TemporaryDirectory() as td:
            source, output, report = self.fixture(Path(td))
            expected = verification.verify(source, output, copy.deepcopy(report))
            for available in (0, 32*1024**3):
                stats = {}
                with patch.object(verification, 'available_memory', return_value=available), \
                     patch.object(verification.mp, 'get_context', side_effect=AssertionError('unexpected pool')):
                    actual = verification.verify(source, output, copy.deepcopy(report), workers=16, stats=stats)
                self.assertEqual(actual, expected)
                self.assertEqual(stats['workers'], 0)
                self.assertEqual(stats['serial_pages'], 6)
                self.assertIsNone(stats['fallback'])

    def test_pool_error_discards_partial_findings_and_rechecks_all_pages(self):
        with tempfile.TemporaryDirectory() as td:
            source, output, report = self.fixture(Path(td))
            expected = verification.verify(source, output, copy.deepcopy(report))
            for error in (MemoryError('memory pressure'), TimeoutError('worker timeout'), RuntimeError('worker died')):
                stats = {}
                with patch.object(verification, 'choose_workers', return_value=(2, 1024**3, 'test')), \
                     patch.object(verification, '_parallel', side_effect=error):
                    actual = verification.verify(source, output, copy.deepcopy(report), workers=2, stats=stats)
                self.assertEqual(actual, expected)
                self.assertEqual(stats['serial_pages'], 6)
                self.assertEqual(stats['fallback'], str(error))

    def test_bad_worker_result_terminates_owned_pool_and_rechecks(self):
        class Ready:
            def ready(self):
                return True
            def get(self):
                return 99, {'page': 100}
        class Pool:
            terminated = joined = False
            def apply_async(self, *args):
                return Ready()
            def terminate(self):
                self.terminated = True
            def join(self):
                self.joined = True
        with tempfile.TemporaryDirectory() as td:
            source, output, report = self.fixture(Path(td))
            expected = verification.verify(source, output, copy.deepcopy(report))
            pool, stats = Pool(), {}
            with patch.object(verification, 'choose_workers', return_value=(2, 1024**3, 'test')), \
                 patch.object(verification.mp, 'get_context') as context:
                context.return_value.Pool.return_value = pool
                actual = verification.verify(source, output, report, workers=2, stats=stats)
            self.assertEqual(actual, expected)
            self.assertTrue(pool.terminated and pool.joined)
            self.assertIn('Invalid verification worker result', stats['fallback'])

    def test_page_coverage_missing_inputs_and_page_count_fail_before_pool(self):
        with tempfile.TemporaryDirectory() as td:
            source, output, report = self.fixture(Path(td))
            for kind in ('order', 'missing', 'count'):
                candidate = copy.deepcopy(report)
                if kind == 'order':
                    candidate['pages'].reverse()
                elif kind == 'missing':
                    candidate['pages'][0].pop('expected_characters')
                else:
                    with fitz.open(output) as doc:
                        doc.delete_page(0)
                        shorter = Path(td)/'short.pdf'
                        doc.save(shorter)
                with patch.object(verification.mp, 'get_context', side_effect=AssertionError('premature pool')):
                    with self.assertRaises((RuntimeError, ValueError)):
                        verification.verify(source, shorter if kind == 'count' else output, candidate, workers=2)

    def test_read_error_and_file_change_do_not_publish_partial_verification(self):
        with tempfile.TemporaryDirectory() as td:
            source, output, report = self.fixture(Path(td))
            untouched = copy.deepcopy(report)
            original_check = verification.check_page
            def fail_later(original, result, index, info, debug):
                if index == 2:
                    raise RuntimeError('cannot render')
                return original_check(original, result, index, info, debug)
            with patch.object(verification, 'check_page', side_effect=fail_later):
                with self.assertRaisesRegex(RuntimeError, 'cannot render'):
                    verification.verify(source, output, report)
            self.assertEqual(report, untouched)
            with patch.object(verification, '_stamp', side_effect=[(1, 1, 1), (2, 2, 2), (1, 1, 1), (2, 3, 2)]):
                with self.assertRaisesRegex(RuntimeError, 'PDF changed during verification'):
                    verification.verify(source, output, report)
            self.assertEqual(report, untouched)

    def test_glyph_equivalence_and_nul_checks_are_not_lost_in_extraction(self):
        class Page:
            def get_text(self):
                return '3～5\x00'
            def get_texttrace(self):
                return [{'type': 3}]
        info = {'page': 1, 'warnings': [], 'expected_characters': Counter('3〜5\x00')}
        result = verification.check_page([Page()], [Page()], 0, info, debug=True)
        self.assertEqual(result['warnings'], ['glyph_equivalent_substituted', 'nul_character'])
        self.assertEqual(result['substituted'], {'〜': 1})
        self.assertIs(ocr_workflow.verify, verification.verify)

    def test_custom_aa_settings_do_not_silently_change_in_workers(self):
        with tempfile.TemporaryDirectory() as td:
            source, output, report = self.fixture(Path(td))
            stats = {}
            with patch.object(verification.fitz.TOOLS, 'show_aa_level',
                              return_value={'graphics': 4, 'text': 8, 'graphics_min_line_width': 0}), \
                 patch.object(verification.mp, 'get_context', side_effect=AssertionError('different renderer')):
                verification.verify(source, output, report, workers=2, stats=stats)
            self.assertEqual(stats['admission'], 'custom renderer settings')
            self.assertEqual(stats['serial_pages'], 6)

    def test_timeout_and_interrupt_clean_up_the_owned_pool(self):
        class Pending:
            def __init__(self, interrupt):
                self.interrupt = interrupt
            def ready(self):
                if self.interrupt:
                    raise KeyboardInterrupt()
                return False
        class Pool:
            terminated = joined = False
            def __init__(self, interrupt):
                self.interrupt = interrupt
            def apply_async(self, *args):
                return Pending(self.interrupt)
            def terminate(self):
                self.terminated = True
            def join(self):
                self.joined = True
        with tempfile.TemporaryDirectory() as td:
            source, output, report = self.fixture(Path(td))
            for interrupt in (False, True):
                pool, candidate, stats = Pool(interrupt), copy.deepcopy(report), {}
                with patch.object(verification, 'choose_workers', return_value=(2, 1024**3, 'test')), \
                     patch.object(verification.mp, 'get_context') as context:
                    context.return_value.Pool.return_value = pool
                    if interrupt:
                        with self.assertRaises(KeyboardInterrupt):
                            verification.verify(source, output, candidate, workers=2, stats=stats)
                        self.assertEqual(candidate, report)
                    else:
                        verification.verify(source, output, candidate, workers=2, stats=stats, timeout=1e-9)
                        self.assertIn('timed out', stats['fallback'])
                        self.assertEqual(stats['serial_pages'], 6)
                self.assertTrue(pool.terminated and pool.joined)

    def test_real_initializer_error_returns_without_respawn_loop(self):
        with tempfile.TemporaryDirectory() as td:
            source, output, report = self.fixture(Path(td))
            real_context, stats = verification.mp.get_context('spawn'), {}
            def broken_initialization(*args, **kwargs):
                initargs = list(kwargs['initargs'])
                initargs[0] = str(source)+'.missing'
                kwargs['initargs'] = tuple(initargs)
                return real_context.Pool(*args, **kwargs)
            expected = verification.verify(source, output, copy.deepcopy(report))
            with patch.object(verification, 'MIN_PARALLEL_PIXELS', 0), \
                 patch.object(verification.mp, 'get_context') as context:
                context.return_value.Pool.side_effect = broken_initialization
                verification.verify(source, output, report, workers=2, stats=stats)
            self.assertEqual(report, expected)
            self.assertIsNotNone(stats['fallback'])
            self.assertEqual(stats['pool_pages'], 0)
            self.assertEqual(stats['serial_pages'], 6)


if __name__ == '__main__':
    unittest.main()
