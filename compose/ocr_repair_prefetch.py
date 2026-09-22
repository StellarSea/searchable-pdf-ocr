"""One HTTP call overlaps owner-side repair preparation; commits stay ordered.

Only PNG bytes enter the HTTP worker. Metadata, PDF objects and checkpoint
callbacks stay on the caller. At most one request is active; the caller may
prepare one next crop before submit() waits for the previous result.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import time


class RepairPrefetch:
    def __init__(self, recognize, apply, on_error, *, enabled=False):
        self.recognize, self.apply, self.on_error = recognize, apply, on_error
        self.owner = threading.get_ident()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ocr-repair-http') if enabled else None
        self.pending = None
        self.stats = {'requests': 0, 'wait_seconds': 0.0, 'errors': 0}

    def _owner(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError('Repair results must be consumed by their owner')

    def __enter__(self):
        self._owner()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._owner()
        try:
            # An earlier request must be audited/checkpointed before a later
            # preparation failure escapes, just as in the serial workflow.
            self.finish()
        finally:
            if self.executor is not None:
                self.executor.shutdown(wait=True, cancel_futures=True)

    def _error(self, metadata, error):
        self.stats['errors'] += 1
        self.on_error(metadata, error)

    def poll(self):
        self._owner()
        if self.pending is not None and self.pending[1].done():
            self.finish()

    def finish(self):
        self._owner()
        if self.pending is None:
            return
        metadata, future = self.pending
        self.pending = None
        started = time.perf_counter()
        try:
            try:
                result = future.result()
            finally:
                self.stats['wait_seconds'] += time.perf_counter() - started
        except Exception as error:
            self._error(metadata, error)
        else:
            self.apply(metadata, result)

    def fail(self, metadata, error):
        self._owner()
        self.finish()
        self._error(metadata, error)

    def submit(self, metadata, png):
        self._owner()
        # Never dispatch the next request until the previous checkpoint succeeds.
        self.finish()
        self.stats['requests'] += 1
        if self.executor is not None:
            try:
                future = self.executor.submit(self.recognize, png)
            except Exception as error:
                self._error(metadata, error)
            else:
                self.pending = metadata, future
        else:
            try:
                result = self.recognize(png)
            except Exception as error:
                self._error(metadata, error)
            else:
                self.apply(metadata, result)
