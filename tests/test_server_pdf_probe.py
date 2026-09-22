"""The CPU experiment must not leak shared memory on success or failure."""
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import shared_memory
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import server_pdf_parallel_probe as probe


class ServerPDFProbeTests(unittest.TestCase):
    def run_case(self, fail_worker=False, fail_submit=False):
        constructor = shared_memory.SharedMemory
        names, completed = [], []
        began = threading.Event()

        def allocate(*args, **kwargs):
            segment = constructor(*args, **kwargs)
            if kwargs.get('create'):
                names.append(segment.name)
            return segment

        def worker(task):
            index, name, shape, dtype = task
            began.set()
            # Work must be joined before the owner unlinks even on submit errors.
            time.sleep(0.03)
            segment = constructor(name=name)
            try:
                view = np.ndarray(shape, dtype=dtype, buffer=segment.buf)
                view.fill(index + 1)
                del view
            finally:
                segment.close()
            completed.append(index)
            if fail_worker and index == 0:
                raise RuntimeError('worker failed')
            return index

        with ThreadPoolExecutor(max_workers=2) as executor:
            class Pool:
                calls = 0

                def submit(self, callback, task):
                    self.calls += 1
                    if fail_submit and self.calls == 2:
                        self_outer.assertTrue(began.wait(2))
                        raise RuntimeError('submit failed')
                    return executor.submit(callback, task)

            self_outer = self
            reference = [([2, 3, 3], 'uint8', ''), ([3, 2, 3], 'uint8', '')]
            with patch.object(probe, 'render_shared', side_effect=worker), \
                    patch.object(probe.shared_memory, 'SharedMemory', side_effect=allocate):
                if fail_worker or fail_submit:
                    with self.assertRaises(RuntimeError):
                        probe.shared_images(Pool(), reference)
                else:
                    images = probe.shared_images(Pool(), reference)
                    for index, image in enumerate(images):
                        self.assertTrue(image.flags.owndata)
                        self.assertIsNone(image.base)
                        self.assertTrue((image == index + 1).all())
                # Check while the executor still exists, not after its cleanup.
                self.assertEqual([0] if fail_submit else [0, 1], sorted(completed))
                for name in names:
                    with self.assertRaises(FileNotFoundError):
                        constructor(name=name)

    def test_success_returns_owned_arrays(self):
        self.run_case()

    def test_worker_error_drains_before_unlink(self):
        self.run_case(fail_worker=True)

    def test_partial_submit_drains_before_unlink(self):
        self.run_case(fail_submit=True)


if __name__ == '__main__':
    unittest.main()
