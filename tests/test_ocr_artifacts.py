"""Compact storage: verified migration, legacy reads, resume and safe export."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
from ocr_artifacts import TextArtifact, import_sidecars, resolve_text, export_artifacts
from ocr_storage import BoundaryCache, atomic_json
import ocr_workflow as workflow
import ocr_batch
import audit_ocr_quality as audit
import test_ocr_automatic as automatic_tests


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.internal = self.root/'.ocr'
        self.internal.mkdir()
        self.database = self.internal/'book_line_ocr.sqlite3'

    def test_import_preserves_bytes_and_existing_sql_tables_and_exports(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE responses (key TEXT PRIMARY KEY, value TEXT)')
            db.execute("INSERT INTO responses VALUES ('image', '문자 원문')")
        files = {'book_pruned.json': b'[ {"text": "original"} ]\r\n',
                 'book.md': '원문\r\n'.encode('utf-8')}
        for name, data in files.items():
            (self.internal/name).write_bytes(data)
        removed = workflow.compact_output(self.root)
        self.assertEqual(len(removed), 2)
        for name, data in files.items():
            self.assertFalse((self.internal/name).exists())
            self.assertEqual(workflow.find_artifact(self.root, name).read_bytes(), data)
            self.assertEqual(resolve_text(self.internal/name).read_bytes(), data)
        self.assertEqual(workflow.compact_output(self.root), [])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT * FROM responses').fetchall(), [('image', '문자 원문')])
        destination = self.root/'export'
        self.assertEqual(export_artifacts(self.database, destination), 2)
        for name, data in files.items():
            self.assertEqual((destination/name).read_bytes(), data)
        (destination/'book.md').write_text('user changes')
        with self.assertRaises(FileExistsError):
            export_artifacts(self.database, destination)

    def test_conflict_rolls_back_without_deleting_any_input(self):
        existing = TextArtifact(self.database, 'book.md')
        existing.write_text('new')
        a, b = self.internal/'book_pruned.json', self.internal/'book.md'
        a.write_text('[]'); b.write_text('old')
        with self.assertRaisesRegex(RuntimeError, 'preserved both'):
            import_sidecars(self.database, [(a, a.name), (b, b.name)], self.root)
        self.assertTrue(a.exists()); self.assertTrue(b.exists())
        self.assertFalse(TextArtifact(self.database, a.name).exists())
        self.assertEqual(existing.read_text(), 'new')

    def test_validation_failure_retains_original(self):
        path = self.internal/'book.md'
        path.write_text('original')
        with patch.object(TextArtifact, 'read_bytes', return_value=b'corrupt'):
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                import_sidecars(self.database, [(path, path.name)], self.root)
        self.assertEqual(path.read_text(), 'original')

    def test_active_document_is_not_migrated(self):
        path = self.internal/'book_pruned.json'
        path.write_text('[]')
        with workflow.output_lock(self.root, 'book'):
            with self.assertRaisesRegex(RuntimeError, 'already being processed'):
                workflow.compact_output(self.root)
        self.assertTrue(path.exists())
        self.assertFalse(self.database.exists())

    def test_boundary_shards_use_one_database_and_resume(self):
        entry = TextArtifact(self.database, 'book_boundary_cache.json')
        cache = BoundaryCache(entry, {'source': 'sha', 'version': 'unchanged'})
        cache.save_page(2, 'fingerprint', {'text': 'kept'})
        resumed = BoundaryCache(entry, cache.identity)
        self.assertEqual(resumed.get(2, 'fingerprint'), {'text': 'kept'})
        resumed.finish()
        self.assertEqual({p.name for p in self.internal.iterdir()}, {self.database.name})
        self.assertEqual(json.loads(entry.read_text())['pages']['2']['result'], {'text': 'kept'})

    def test_unsafe_key_and_outside_file_are_not_removed(self):
        for key in ('../escape.json', '/absolute.json', 'C:/file.json', 'a\\b.json'):
            with self.assertRaises(ValueError):
                TextArtifact(self.database, key)
        outside = self.root/'outside.json'
        outside.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Unsafe migration target'):
            import_sidecars(self.database, [(outside, 'book.json')], self.internal)
        self.assertTrue(outside.exists())

    def test_compact_cli_legacy_migration_keeps_output_and_cache_only_resume(self):
        helper = automatic_tests.AutomaticTests()
        source = self.root/'book.pdf'
        helper.scanned(source, 2)
        api, lines, _, _ = helper.responder()
        helper.run_cli(source, api, lines)
        out = self.root/'ocr_output'
        database = out/'.ocr'/'book_line_ocr.sqlite3'
        pdf = out/'book_auto_searchable.pdf'
        with fitz.open(pdf) as doc:
            before = [(p.get_text('rawdict'), p.get_pixmap().samples) for p in doc]
        # Simulate genuine legacy sidecars while preserving model/cache tables.
        export_artifacts(database, out/'.ocr')
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('DELETE FROM artifacts')
        fail = lambda *a, **k: self.fail('Migration lost an OCR cache')
        helper.run_cli(source, fail, fail)
        self.assertEqual({p.name for p in (out/'.ocr').iterdir()}, {'book_line_ocr.sqlite3', 'book.lock'})
        self.assertTrue(ocr_batch.finished(source, out))
        result = audit.audit_one(pdf)
        self.assertTrue(result['coverage']['checked_against_layout'])
        self.assertGreater(result['agreement']['compared'], 0)
        with fitz.open(pdf) as doc:
            self.assertEqual(before, [(p.get_text('rawdict'), p.get_pixmap().samples) for p in doc])
