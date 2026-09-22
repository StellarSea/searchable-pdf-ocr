"""Event-controlled overlap, ordered commits and failures; no server or sleeps."""
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
from ocr_repair_prefetch import RepairPrefetch


class RepairPrefetchTests(unittest.TestCase):
    def test_owner_prepares_while_http_is_active_then_commits_in_order(self):
        started, prepared = threading.Event(), threading.Event()
        owner, calls, commits = threading.get_ident(), [], []

        def recognize(png):
            self.assertNotEqual(threading.get_ident(), owner)
            self.assertIsInstance(png, bytes)
            calls.append(png)
            if png == b'first':
                started.set()
                self.assertTrue(prepared.wait(5), 'CPU preparation did not overlap')
            else:
                self.assertEqual(commits, [('one', 'first')])
            return png.decode()

        def apply(meta, result):
            self.assertEqual(threading.get_ident(), owner)
            commits.append((meta, result))

        def error(meta, ex):
            raise ex

        with RepairPrefetch(recognize, apply, error, enabled=True) as feed:
            feed.submit('one', b'first')
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(commits, [])
            finally:
                prepared.set()
            feed.submit('two', b'second')
        self.assertEqual(calls, [b'first', b'second'])
        self.assertEqual(commits, [('one', 'first'), ('two', 'second')])
        self.assertIsNone(feed.pending)

    def test_failed_request_stops_before_next_dispatch_when_checkpoint_requires_it(self):
        calls, errors = [], []
        def recognize(png):
            calls.append(png)
            raise ValueError('request failed')
        def error(meta, ex):
            errors.append(meta)
            raise ex
        with self.assertRaisesRegex(ValueError, 'request failed'):
            with RepairPrefetch(recognize, lambda *args: self.fail('applied failure'), error, enabled=True) as feed:
                feed.submit(1, b'one')
                feed.submit(2, b'two')
        self.assertEqual(calls, [b'one'])
        self.assertEqual(errors, [1])

    def test_checkpoint_failure_is_not_misreported_as_ocr_failure_or_retried(self):
        calls = []
        def recognize(png):
            calls.append(png)
            return 'text'
        def apply(meta, result):
            raise RuntimeError('checkpoint failed')
        with self.assertRaisesRegex(RuntimeError, 'checkpoint failed'):
            with RepairPrefetch(recognize, apply, lambda *args: self.fail('wrong error owner'), enabled=True) as feed:
                feed.submit(1, b'one')
                feed.submit(2, b'two')
        self.assertEqual(calls, [b'one'])

    def test_later_prepare_failure_still_commits_earlier_request(self):
        commits = []
        with self.assertRaisesRegex(ValueError, 'prepare failed'):
            with RepairPrefetch(lambda png: 'text', lambda *args: commits.append(args),
                                lambda *args: self.fail('unexpected API failure'), enabled=True) as feed:
                feed.submit(1, b'one')
                raise ValueError('prepare failed')
        self.assertEqual(commits, [(1, 'text')])

    def test_render_failure_order_and_nonfatal_api_failure(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                events = []
                def recognize(png):
                    if png == b'bad':
                        raise ValueError('API failed')
                    return png.decode()
                with RepairPrefetch(recognize,
                        lambda meta, result: events.append((meta, result)),
                        lambda meta, ex: events.append((meta, str(ex))), enabled=enabled) as feed:
                    feed.submit(1, b'good')
                    feed.fail(2, RuntimeError('render failed'))
                    feed.submit(3, b'bad')
                    feed.submit(4, b'last')
                self.assertEqual(events, [(1, 'good'), (2, 'render failed'), (3, 'API failed'), (4, 'last')])
                self.assertEqual(feed.stats['errors'], 2)

    def test_poll_publishes_finished_response_without_another_request(self):
        commits = []
        with RepairPrefetch(lambda png: 'text', lambda *args: commits.append(args),
                            lambda *args: self.fail('unexpected failure'), enabled=True) as feed:
            feed.submit(1, b'one')
            feed.pending[1].result(timeout=5)
            feed.poll()
            self.assertEqual(commits, [(1, 'text')])
            self.assertIsNone(feed.pending)


if __name__ == '__main__':
    unittest.main()
