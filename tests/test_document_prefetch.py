"""Document overlap changes scheduling, not input, ordering or resume identity."""
from collections import Counter
from contextlib import closing, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pymupdf as fitz
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
from ocr_document_prefetch import document_results
from ocr_artifacts import TextArtifact
import ocr_to_searchable_pdf as ocr


def response(numbers):
    return {'layoutParsingResults': [
        {'markdown': {'text': f'page {n}'}, 'prunedResult': {'page': n}}
        for n in numbers]}


class DocumentPrefetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_overlap_out_of_order_owner_and_two_chunk_bound(self):
        owner, entered, second_done = threading.get_ident(), threading.Event(), threading.Event()
        generated, finished, calls, stats = [], [], [], {}
        def chunks():
            try:
                for n in range(5):
                    self.assertEqual(threading.get_ident(), owner)
                    self.assertLessEqual(len(generated)-len(finished), 1)
                    generated.append(n)
                    yield n, bytes([n]), 5
            finally:
                calls.append('closed')
        def recognize(data):
            n = data[0]
            self.assertNotEqual(threading.get_ident(), owner)
            if n == 0:
                entered.set()
                self.assertTrue(second_done.wait(3), 'second request did not overlap')
            if n == 1:
                self.assertTrue(entered.wait(3))
                second_done.set()
            return n
        with closing(document_results(chunks(), 1, 0, recognize, stats=stats)) as results:
            for start, value, total in results:
                self.assertEqual(threading.get_ident(), owner)
                self.assertEqual(start, value)
                finished.append(start)
        self.assertEqual(finished, list(range(5)))
        self.assertEqual(calls, ['closed'])
        self.assertEqual(stats['peak_pending'], 2)

    def test_actual_pdf_chunks_and_results_preserved(self):
        source = self.root/'source.pdf'
        with fitz.open() as doc:
            for n in range(5):
                doc.new_page(width=180, height=220).insert_text((20, 40), f'Page {n}')
            doc.save(source)
        chunks = list(ocr.split_pdf(source, 2))
        expected = {hashlib.sha256(data).hexdigest(): response(range(start, min(start+2, total)))
                    for start, data, total in chunks}
        runs, seen = [], []
        owner = threading.get_ident()
        for concurrency in (1, 2):
            captured = []
            def recognize(data):
                captured.append(data)
                return expected[hashlib.sha256(data).hexdigest()]
            def save(path, value):
                self.assertEqual(threading.get_ident(), owner)
                ocr_storage_atomic(path, value)
            ocr_storage_atomic = ocr.atomic_json
            with patch.object(ocr, 'split_pdf', return_value=iter(chunks)), \
                 patch.object(ocr, 'ocr_pdf', recognize), patch.object(ocr, 'atomic_json', save), \
                 redirect_stdout(io.StringIO()):
                runs.append(ocr.run_ocr(source, 2, self.root/f'{concurrency}.json', {},
                                        document_requests=concurrency))
            seen.append(Counter(hashlib.sha256(data).hexdigest() for data in captured))
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(json.loads((self.root/'1.json').read_text()),
                         json.loads((self.root/'2.json').read_text()))
        with fitz.open(source) as original:
            for start, data, total in chunks:
                with fitz.open(stream=data, filetype='pdf') as part:
                    for n, page in enumerate(part):
                        self.assertEqual(page.get_text('rawdict'), original[start+n].get_text('rawdict'))
                        self.assertEqual(page.get_pixmap().samples, original[start+n].get_pixmap().samples)

    def test_memory_admission_and_oversized_chunk_run_alone(self):
        active, peak, lock, stats = 0, 0, threading.Lock(), {}
        def recognize(data):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.01)
            with lock:
                active -= 1
            return data
        chunks = [(n, b'x'*size, 4) for n, size in enumerate((4, 4, 20, 4))]
        results = list(document_results(chunks, 1, 0, recognize, budget=24, stats=stats))
        self.assertEqual(peak, 1)
        self.assertEqual([value for _, value, _ in results], [row[1] for row in chunks])
        self.assertGreater(stats['budget_drains'], 0)

    def test_failed_request_drains_then_retries_serially(self):
        counts, active, lock, second_started = Counter(), set(), threading.Lock(), threading.Event()
        stats = {}
        def recognize(data):
            n = data[0]
            with lock:
                counts[n] += 1
                attempt = counts[n]
                if n == 0 and attempt == 2:
                    self.assertEqual(active, set(), 'retry overlapped outstanding request')
                active.add(n)
            try:
                if n == 0 and attempt == 1:
                    self.assertTrue(second_started.wait(3))
                    raise RuntimeError('busy')
                if n == 1:
                    second_started.set()
                    time.sleep(.03)
                return n
            finally:
                with lock:
                    active.remove(n)
        with redirect_stdout(io.StringIO()):
            values = list(document_results([(n, bytes([n]), 4) for n in range(4)],
                                            1, 0, recognize, stats=stats))
        self.assertEqual([v for _, v, _ in values], list(range(4)))
        self.assertEqual(counts, {0: 2, 1: 1, 2: 1, 3: 1})
        self.assertEqual(stats['fallbacks'], ['busy'])

    def test_invalid_result_keeps_sqlite_checkpoint_contiguous_and_resumable(self):
        cp = TextArtifact(self.root/'book_line_ocr.sqlite3', 'partial.json')
        chunks = [(n, bytes([n]), 4) for n in range(4)]
        def bad(data):
            return response([] if data[0] == 1 else [data[0]])
        with patch.object(ocr, 'split_pdf', return_value=iter(chunks)), \
             patch.object(ocr, 'ocr_pdf', bad), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'page count mismatch'):
                ocr.run_ocr(Path('unused'), 1, cp, {}, document_requests=2)
        self.assertEqual(json.loads(cp.read_text())['pages'], [{'page': 0}])
        seen = []
        def good(data):
            seen.append(data[0])
            return response([data[0]])
        with patch.object(ocr, 'split_pdf', return_value=iter(chunks)), \
             patch.object(ocr, 'ocr_pdf', good), redirect_stdout(io.StringIO()):
            md, pages = ocr.run_ocr(Path('unused'), 1, cp, {}, document_requests=2)
        self.assertEqual(sorted(seen), [1, 2, 3])
        self.assertEqual(pages, [{'page': n} for n in range(4)])

    def test_bad_checkpoint_rejected_before_any_request(self):
        calls = []
        with self.assertRaisesRegex(RuntimeError, 'Incomplete batch'):
            list(document_results([(0, b'a', 4)], 2, 1, calls.append))
        self.assertEqual(calls, [])

    def test_persistent_failure_preserves_prefix_for_restart(self):
        cp = self.root/'failed.json'
        chunks = [(n, bytes([n]), 3) for n in range(3)]
        calls = Counter()
        def recognize(data):
            calls[data[0]] += 1
            if data[0] == 1:
                raise RuntimeError('persistent transport failure')
            return response([data[0]])
        with patch.object(ocr, 'split_pdf', return_value=iter(chunks)), \
             patch.object(ocr, 'ocr_pdf', recognize), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'persistent transport'):
                ocr.run_ocr(Path('unused'), 1, cp, {}, document_requests=2)
        self.assertEqual(json.loads(cp.read_text())['pages'], [{'page': 0}])
        self.assertEqual(calls[1], 2)  # Only one scheduler-level serial retry.

    def test_whole_document_mode_is_unchanged(self):
        source = self.root/'whole.pdf'
        with fitz.open() as doc:
            doc.new_page()
            doc.save(source)
        owner = threading.get_ident()
        def recognize(data):
            self.assertEqual(threading.get_ident(), owner)
            self.assertEqual(data, source.read_bytes())
            return response([0])
        with patch.object(ocr, 'ocr_pdf', recognize), redirect_stdout(io.StringIO()):
            self.assertEqual(ocr.run_ocr(source, 0, document_requests=2)[1], [{'page': 0}])

    def test_invalid_limits_and_sequence_fail(self):
        for kwargs in ({'requests': 0}, {'requests': 3}, {'budget': 0}):
            with self.assertRaises(ValueError):
                list(document_results([], 1, 0, lambda x: x, **kwargs))
        for chunks in ([(1, b'x', 2)], [(0, b'x', 3), (1, b'y', 4)], [(0, b'x', 3)]):
            with self.assertRaises(RuntimeError):
                list(document_results(chunks, 1, 0, lambda x: x))

    def test_permanent_http_error_is_not_retried(self):
        calls = []
        def recognize(data):
            calls.append(data)
            result = requests.Response()
            result.status_code = 400
            raise requests.HTTPError(response=result)
        with self.assertRaises(requests.HTTPError):
            list(document_results([(0, b'x', 1)], 1, 0, recognize))
        self.assertEqual(calls, [b'x'])

    def test_serial_optout_and_completed_resume_make_no_speculative_calls(self):
        owner, calls, generated = threading.get_ident(), [], []
        def chunks():
            for n in range(3):
                self.assertEqual(len(calls), n)
                generated.append(n)
                yield n, bytes([n]), 3
        def recognize(data):
            self.assertEqual(threading.get_ident(), owner)
            calls.append(data[0])
            return data[0]
        list(document_results(chunks(), 1, 0, recognize, requests=1))
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(list(document_results([(0, b'a', 1)], 1, 1, recognize)), [])
        default = ocr.build_parser().parse_args(['book.pdf'])
        off = ocr.build_parser().parse_args(['book.pdf', '--ocr-requests', '1'])
        self.assertEqual(default.ocr_requests, 2)
        self.assertEqual(ocr.run_options(default), ocr.run_options(off))

    def test_early_close_closes_source_and_joins_outstanding_work(self):
        closed, finished = [], threading.Event()
        def chunks():
            try:
                yield 0, b'a', 3
                yield 1, b'b', 3
                self.fail('read beyond bounded window')
            finally:
                closed.append(threading.get_ident())
        def recognize(data):
            if data == b'b':
                time.sleep(.02)
                finished.set()
            return data
        stream = document_results(chunks(), 1, 0, recognize)
        next(stream)
        stream.close()
        self.assertEqual(closed, [threading.get_ident()])
        # The second future can either be cancelled before starting or joined.
        self.assertFalse(any(t.name.startswith('ocr-document') for t in threading.enumerate()))


if __name__ == '__main__':
    unittest.main()
