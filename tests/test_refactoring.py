"""Structural and failure-path guarantees that do not require OCR services."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'compose'))
sys.path.insert(0, str(ROOT/'tools'))
import ocr_to_searchable_pdf as ocr
import ocr_storage
from check_architecture import import_graph, validate_graph, undefined_globals


class RefactoringTests(unittest.TestCase):
    def test_missing_import_in_rare_branch_is_detected_without_execution(self):
        code = 'from math import ceil\nVALUE = 1\ndef render(items):\n    if items:\n        return split_text(items, ceil(VALUE))\n'
        self.assertEqual(undefined_globals(code), ['split_text'])
        self.assertEqual(undefined_globals('from ocr_text import split_text\n'+code), [])

    def test_all_implementation_globals_are_bound(self):
        for path in (ROOT/'compose').glob('*.py'):
            with self.subTest(path=path.name):
                self.assertEqual(undefined_globals(path.read_text(encoding='utf-8'), str(path)), [])

    def test_implementation_graph_is_acyclic(self):
        validate_graph(import_graph(ROOT/'compose'))

    def test_architecture_guard_rejects_cycles_and_reverse_dependencies(self):
        with self.assertRaisesRegex(ValueError, 'Circular import'):
            validate_graph({'a': {'b'}, 'b': {'a'}})
        with self.assertRaisesRegex(ValueError, 'must not import'):
            validate_graph({'ocr_text': {'ocr_to_searchable_pdf'}, 'ocr_to_searchable_pdf': set()})

    def test_dispatcher_restores_argv_on_failure(self):
        spec = importlib.util.spec_from_file_location('refactor_entrypoint', ROOT/'run.py')
        entrypoint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entrypoint)
        previous = sys.argv
        def fail():
            self.assertEqual(sys.argv[1:], ['missing.pdf', '--fast'])
            raise RuntimeError('injected')
        with patch.object(entrypoint.importlib, 'import_module', return_value=Mock(main=fail)) as load:
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                entrypoint.main(['missing.pdf', '--fast'])
            load.assert_called_once_with('ocr_to_searchable_pdf')
        self.assertIs(sys.argv, previous)

    def test_dispatcher_propagates_batch_failure_and_accepts_no_return(self):
        spec = importlib.util.spec_from_file_location('refactor_entrypoint', ROOT/'run.py')
        entrypoint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entrypoint)
        previous = sys.argv
        for result, expected in ((1, 1), (0, 0), (None, 0)):
            with self.subTest(child_result=result), \
                    patch.object(entrypoint.importlib, 'import_module',
                                 return_value=Mock(main=Mock(return_value=result))) as load:
                self.assertEqual(entrypoint.main([str(ROOT), '--stop-on-failure']), expected)
                load.assert_called_once_with('ocr_batch')
            self.assertIs(sys.argv, previous)

    def test_unpublished_decisions_cannot_replace_previous_audit(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'lines.json'
            cache = ocr_storage.LineCache(path, automatic=True)
            cache.save_responses({'image-key': 'unchanged'})
            cache.append_decision({'original': 'published'})
            cache.publish_decisions()
            cache.close()
            cache = ocr_storage.LineCache(path, automatic=True)
            cache.append_decision({'original': 'unfinished'})
            cache.close()
            with closing(sqlite3.connect(path.with_suffix('.sqlite3'))) as db:
                self.assertEqual(json.loads(db.execute('SELECT value FROM decisions').fetchone()[0]),
                                 {'original': 'published'})
                self.assertEqual(db.execute('SELECT key,value FROM responses').fetchone(),
                                 ('image-key', 'unchanged'))

    def test_cache_initialization_failure_closes_connection(self):
        with tempfile.TemporaryDirectory() as td:
            connection = Mock()
            connection.execute.side_effect = RuntimeError('bad schema')
            with patch.object(ocr_storage.sqlite3, 'connect', return_value=connection):
                with self.assertRaisesRegex(RuntimeError, 'bad schema'):
                    ocr_storage.LineCache(Path(td)/'lines.json', automatic=True)
            connection.close.assert_called_once()

    def test_invalid_review_does_not_open_or_modify_cache(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'review.json'
            path.write_text('invalid json', encoding='utf-8')
            with patch.object(ocr_storage.sqlite3, 'connect') as connect:
                with self.assertRaises(ValueError):
                    ocr.LineRefiner(Path(td)/'lines.json', {1}, path, automatic=True)
            connect.assert_not_called()

    def test_overlay_failure_closes_source_document(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td)/'source.pdf'
            with fitz.open() as doc:
                doc.new_page(width=200, height=100)
                doc.save(source)
            opened = []
            actual_open = fitz.open
            def tracked_open(*args, **kwargs):
                doc = actual_open(*args, **kwargs)
                opened.append(doc)
                return doc
            layout = [{'width': 200, 'height': 100, 'parsing_res_list': [
                {'block_content': 'Original', 'block_bbox': [10, 10, 180, 80]}]}]
            with patch.object(ocr.fitz, 'open', side_effect=tracked_open), \
                 patch.object(ocr, 'detect_lines', side_effect=RuntimeError('injected')):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    ocr.overlay(source, layout, Path(td)/'output.pdf')
            self.assertTrue(opened)
            self.assertTrue(all(doc.is_closed for doc in opened))

    def test_repair_failure_closes_source_document(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td)/'source.pdf'
            with fitz.open() as doc:
                doc.new_page(width=200, height=100)
                doc.save(source)
            handle = fitz.open(source)
            layout = [{'width': 200, 'height': 100, 'parsing_res_list': [
                {'block_content': 'Original', 'block_bbox': [10, 10, 180, 80]}]}]
            with patch.object(ocr.fitz, 'open', return_value=handle), \
                 patch.object(ocr, 'detect_lines', side_effect=RuntimeError('injected')):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    ocr.repair_blocks(source, layout)
            self.assertTrue(handle.is_closed)


if __name__ == '__main__':
    unittest.main()
