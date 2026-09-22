"""Reusable CPU PDF reader lifecycle tests; no PaddleX, GPU or HTTP required."""
from multiprocessing import shared_memory
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
from ocr_server_pdf import MIB, ParallelPDFReader
import ocr_server_entrypoint


def ready(queue):
    queue.put((os.getpid(), None))


def broken_ready(queue):
    queue.put((os.getpid(), 'initialization failed'))


def render(task):
    index, source_name, length, output_name, expected = task
    source = shared_memory.SharedMemory(name=source_name)
    try:
        data = bytes(source.buf[:length])
    finally:
        source.close()
    if data == b'!':
        raise RuntimeError('render failed')
    if data == b'Z':
        time.sleep(60)
    if data == b'X':
        os._exit(3)
    if data == b'?':
        return index, (1, 1, 3)
    if data == b'F' and index >= 3:
        raise RuntimeError('later window failed')
    if data == b'S' and index == 0:
        time.sleep(0.15)
    output = shared_memory.SharedMemory(name=output_name)
    shape = (*expected, 3)
    try:
        image = np.ndarray(shape, dtype=np.uint8, buffer=output.buf)
        image.fill(data[0] + index)
        del image
    finally:
        output.close()
    return index, shape


class ServerPDFTests(unittest.TestCase):
    def test_admission_reduces_window_but_budgets_all_pool_interpreters(self):
        reader = self.reader()
        reader.workers = 4
        reader.memory = lambda: (2048 + 2800) * MIB
        reader.shm_capacity = lambda: 200 * MIB
        self.assertTrue(reader._admit(b'x' * MIB, [(4096, 4096)] * 10))
        self.assertEqual(reader.last_stats['window_pages'], 2)
        self.assertEqual(reader.last_stats['reservation_bytes'], 2692 * MIB)
        self.assertEqual(reader.workers, 4)
        reader.plan = lambda data, limit: [(8, 6)] * 8
        def admit(data, shapes):
            reader.last_stats['window_pages'] = 2
            return True
        reader._admit = admit
        images, _ = reader(b'a')
        self.assertEqual(len(images), 8)
        self.assertEqual(reader.last_stats['peak_pending_pages'], 2)
        self.assertEqual(len(reader.pool._pool), 4)

    def test_window_bounds_shared_outputs_and_preserves_out_of_order_pages(self):
        reader = self.reader()
        reader.plan = lambda data, limit: [(8 + i, 6) for i in range(70)]
        reader._admit = lambda data, shapes: True
        constructor = shared_memory.SharedMemory
        active, peak, created = set(), 0, []

        def allocate(*args, **kwargs):
            nonlocal peak
            segment = constructor(*args, **kwargs)
            if kwargs.get('create'):
                name = segment.name
                active.add(name)
                created.append(name)
                peak = max(peak, len(active))
                unlink = segment.unlink
                def release():
                    unlink()
                    active.remove(name)
                segment.unlink = release
            return segment

        with patch('ocr_server_pdf.shared_memory.SharedMemory', side_effect=allocate):
            images, info = reader(b'S')
        self.assertEqual(peak, reader.workers + 1)  # source + bounded outputs
        self.assertFalse(active)
        self.assertEqual(len(created), 71)
        self.assertEqual(reader.last_stats['peak_pending_pages'], reader.workers)
        self.assertEqual(len(images), 70)
        for i, image in enumerate(images):
            self.assertTrue(image.flags.owndata)
            self.assertEqual(list(image.shape), [8 + i, 6, 3])
            self.assertTrue((image == ord('S') + i).all())
        for name in created:
            with self.assertRaises(FileNotFoundError):
                constructor(name=name)

    def test_later_window_failure_discards_partial_images_and_cleans_up(self):
        reader = self.reader()
        reader.plan = lambda data, limit: [(8, 6)] * 8
        reader._admit = lambda data, shapes: True
        self.no_segments_left(lambda: self.assertEqual(reader(b'F'), ([], 'serial')))
        self.assertIn('later window failed', reader.disabled)
        self.assertIsNone(reader.pool)
        reader.serial.assert_called_once_with(b'F', max_num_imgs=None)

    def test_window_admits_large_batches_without_all_page_shared_storage(self):
        reader = self.reader()
        shapes = [(4096, 4096)] * 10
        total = 10 * 64 * MIB
        old_reserve = 2 * total + reader.workers * (64 * MIB + 384 * MIB) + 258 * MIB
        reader.memory = lambda: old_reserve - 128 * MIB + 2048 * MIB
        reader.shm_capacity = lambda: 200 * MIB
        self.assertTrue(reader._admit(b'x' * MIB, shapes))
        self.assertLess(reader.last_stats['reservation_bytes'], old_reserve - 128 * MIB)
        self.assertEqual(reader.last_stats['shared_bytes'], 129 * MIB)
        reader.memory = lambda: reader.last_stats['reservation_bytes'] + 2048 * MIB - 1
        self.assertFalse(reader._admit(b'x' * MIB, shapes))

    def reader(self, **kwargs):
        serial = Mock(return_value=([], 'serial'))
        reader = ParallelPDFReader(serial, workers=2, timeout=5,
            plan=lambda data, limit: [(8, 6), (7, 5)][:limit], render=render,
            initializer=ready, make_info=lambda images: [list(image.shape) for image in images],
            memory=lambda: 32 * 1024 * MIB, shm_capacity=lambda: 32 * 1024 * MIB,
            log=lambda value: None, **kwargs)
        self.addCleanup(reader.close)
        return reader

    def no_segments_left(self, callback):
        constructor, names = shared_memory.SharedMemory, []

        def allocate(*args, **kwargs):
            result = constructor(*args, **kwargs)
            if kwargs.get('create'):
                names.append(result.name)
            return result

        with patch('ocr_server_pdf.shared_memory.SharedMemory', side_effect=allocate):
            callback()
        self.assertTrue(names)
        for name in names:
            with self.assertRaises(FileNotFoundError):
                constructor(name=name)

    def test_reuse_across_inputs_returns_owned_arrays(self):
        reader = self.reader()
        self.assertTrue(reader.warmup())
        pool = reader.pool
        reader._admit = lambda data, shapes: True

        def check():
            for data in (b'a', b'b', b'c'):
                images, info = reader(data)
                self.assertTrue(reader.last_stats['parallel'])
                self.assertEqual([[8, 6, 3], [7, 5, 3]], info)
                for index, image in enumerate(images):
                    self.assertTrue(image.flags.owndata)
                    self.assertIsNone(image.base)
                    self.assertTrue((image == data[0] + index).all())
                self.assertIs(pool, reader.pool)
            reader.serial.assert_not_called()
        self.no_segments_left(check)

    def failure(self, data, *, timeout=None):
        reader = self.reader()
        self.assertTrue(reader.warmup())
        reader._admit = lambda data, shapes: True
        if timeout is not None:
            reader.timeout = timeout

        def check():
            self.assertEqual(([], 'serial'), reader(data))
            self.assertFalse(reader.last_stats['parallel'])
            self.assertTrue(reader.last_stats['fallback'])
            self.assertIsNone(reader.pool)
        self.no_segments_left(check)
        self.assertEqual(([], 'serial'), reader(b'next'))
        self.assertIsNone(reader.pool)  # No repeated failure/respawn cycle.

    def test_worker_exception_falls_back(self):
        self.failure(b'!')

    def test_wrong_geometry_falls_back(self):
        self.failure(b'?')

    def test_timeout_terminates_before_unlink(self):
        self.failure(b'Z', timeout=0.2)

    def test_worker_death_terminates_before_unlink(self):
        self.failure(b'X', timeout=0.3)

    def test_partial_submission_terminates_before_unlink(self):
        reader = self.reader()
        reader.warmup()
        reader._admit = lambda data, shapes: True
        submit = reader.pool.apply_async
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('submission failed')
            return submit(*args, **kwargs)

        with patch.object(reader.pool, 'apply_async', side_effect=fail_second):
            self.no_segments_left(lambda: self.assertEqual(([], 'serial'), reader(b'a')))
        self.assertIsNone(reader.pool)

    def test_memory_and_small_page_admission(self):
        reader = self.reader()
        self.assertFalse(reader._admit(b'a', [(8, 6)] * 2))
        big = [(2048, 4096)] * 2
        self.assertTrue(reader._admit(b'a', big))
        reader.memory = lambda: 100 * MIB
        self.assertFalse(reader._admit(b'a', big))
        reader.memory = lambda: 32 * 1024 * MIB
        reader.shm_capacity = lambda: 1
        self.assertFalse(reader._admit(b'a', big))
        self.assertFalse(reader._admit(b'a', big * 70))

    def test_pressure_during_work_recovers(self):
        reader = self.reader()
        reader.warmup()
        reader._admit = lambda data, shapes: True
        reader.memory = lambda: 0
        self.no_segments_left(lambda: self.assertEqual(([], 'serial'), reader(b'a')))
        self.assertIn('memory', reader.disabled)

    def test_init_error_and_closed_reader_are_serial(self):
        reader = self.reader()
        reader.initializer = broken_ready
        self.assertFalse(reader.warmup())
        self.assertIsNone(reader.pool)
        self.assertEqual(([], 'serial'), reader(b'a', max_num_imgs=1))
        reader.serial.assert_called_with(b'a', max_num_imgs=1)
        reader.close()
        self.assertFalse(reader.warmup())

    def test_interrupt_is_not_retried(self):
        reader = self.reader()
        reader.warmup()
        reader._admit = lambda data, shapes: True
        with patch.object(reader, '_parallel', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                reader(b'a')
        self.assertIsNone(reader.pool)
        reader.serial.assert_not_called()

    def test_disabled_launcher_does_not_import_paddlex(self):
        with patch.dict(os.environ, {'OCR_PDF_WORKERS': '0'}):
            self.assertIsNone(ocr_server_entrypoint.install())

    def test_logging_and_warmup_interrupt_do_not_repeat_work(self):
        reader = self.reader()
        reader.log = Mock(side_effect=BrokenPipeError)
        self.assertEqual(([], 'serial'), reader(b'a'))
        self.assertEqual(1, reader.serial.call_count)
        with patch.object(reader, '_start', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                reader.warmup()
        self.assertIsNone(reader.pool)

    def test_release_allows_future_pdf_and_images_keep_native_path(self):
        reader = self.reader()
        self.assertTrue(reader.warmup())
        pool = reader.pool
        native = Mock(return_value='native result')
        self.assertEqual('native result', ocr_server_entrypoint.decode_with_release(
            reader, native, b'img', 'IMAGE', max_num_imgs=3))
        native.assert_called_once_with(b'img', 'IMAGE', max_num_imgs=3)
        self.assertIsNone(reader.pool)
        self.assertFalse(reader.closed)
        self.assertTrue(reader.warmup())
        self.assertIsNot(pool, reader.pool)
        current_pool = reader.pool
        ocr_server_entrypoint.decode_with_release(reader, native, b'pdf', 'PDF')
        self.assertIs(current_pool, reader.pool)


if __name__ == '__main__':
    unittest.main()
