"""Memory-bounded, process-isolated PDF decoding for the PaddleX HTTP service.

No predictor construction, HTTP, cache or PDF writes. The server supplies the
unchanged serial reader; every failed optimization is discarded before fallback.
Only NumPy arrays owning ordinary memory leave this module.
"""
import multiprocessing as mp
from multiprocessing import shared_memory
import os
import queue
import threading
import time

import numpy as np

MIB = 1024**2
RESERVE = 2048 * MIB


def available_memory():
    try:
        import psutil
        return psutil.virtual_memory().available
    except (ImportError, OSError):
        return 0  # Missing evidence of capacity means serial, not unlimited.


def shared_capacity():
    try:
        stat = os.statvfs('/dev/shm')
        return stat.f_bavail * stat.f_frsize
    except (AttributeError, OSError):
        return 0


def _initialize(ready):
    error = None
    try:
        import pypdfium2
        from paddlex.inference.serving.infra import utils
    except Exception as ex:
        error = str(ex)
    ready.put((os.getpid(), error))  # Never throw and trigger a respawn loop.


def _plan(data, max_num_imgs):
    import pypdfium2 as pdfium
    from paddlex.inference.serving.infra import utils
    from paddlex.inference.utils.pdf_rendering import (
        estimate_pdf_render_pixels, get_pdf_render_scale_within_pixel_limit,
    )
    shapes = []
    with utils.pdfium_lock, pdfium.PdfDocument(data) as doc:
        doc.init_forms()
        for index in range(len(doc)):
            if max_num_imgs is not None and index >= max_num_imgs:
                break
            page = doc[index]
            try:
                scale = utils.PDF_RENDER_SCALE
                if utils.MAX_IMAGE_PIXELS is not None:
                    scale = get_pdf_render_scale_within_pixel_limit(page.get_size(),
                        page_index=index + 1, requested_scale=scale,
                        min_scale=utils.PDF_MIN_RENDER_SCALE, max_pixels=utils.MAX_IMAGE_PIXELS)
                width, height, _ = estimate_pdf_render_pixels(page.get_size(), scale)
                shapes.append((height, width))
            finally:
                page.close()
    return shapes


def _render(task):
    import pypdfium2 as pdfium
    from paddlex.inference.serving.infra import utils
    index, source_name, source_length, output_name, expected = task
    source = shared_memory.SharedMemory(name=source_name)
    try:
        data = bytes(source.buf[:source_length])
    finally:
        source.close()
    with pdfium.PdfDocument(data) as doc:
        doc.init_forms()
        page = doc[index]
        try:
            image = utils.render_pdf_page_to_numpy(page, page_index=index + 1,
                requested_scale=utils.PDF_RENDER_SCALE, rotation=0,
                min_scale=utils.PDF_MIN_RENDER_SCALE, max_pixels=utils.MAX_IMAGE_PIXELS)
            utils.ensure_image_pixel_limit(image, page_index=index + 1)
            if image.shape[:2] != tuple(expected) or image.dtype != np.uint8 or image.shape[2] not in (3, 4):
                raise ValueError('PDF render allocation mismatch')
            output = shared_memory.SharedMemory(name=output_name)
            try:
                view = np.ndarray(image.shape, dtype=image.dtype, buffer=output.buf)
                np.copyto(view, image)
                del view
            finally:
                output.close()
            return index, tuple(image.shape)
        finally:
            page.close()


def _info(images):
    from paddlex.inference.serving.infra.models import PDFInfo, PDFPageInfo
    return PDFInfo(numPages=len(images), pages=[PDFPageInfo(width=image.shape[1], height=image.shape[0])
                                              for image in images])


class ParallelPDFReader:
    def __init__(self, serial, *, workers=4, memory_mb=8192, timeout=60,
                 plan=_plan, render=_render, initializer=_initialize, make_info=_info,
                 memory=available_memory, shm_capacity=shared_capacity, log=print):
        if not 0 <= workers <= 16 or memory_mb < 1 or timeout <= 0:
            raise ValueError('Invalid PDF preparation limits')
        self.serial, self.workers = serial, min(workers, os.cpu_count() or 1)
        self.budget, self.timeout = memory_mb * MIB, timeout
        self.plan, self.render, self.initializer, self.make_info = plan, render, initializer, make_info
        self.memory, self.shm_capacity, self.log = memory, shm_capacity, log
        self.lock = threading.Lock()
        self.pool = self.ready = None
        self.closed = False
        self.disabled = None
        self.last_stats = {}

    def _stop(self):
        pool, ready = self.pool, self.ready
        self.pool = self.ready = None
        if pool is not None:
            pool.terminate()
            pool.join()  # Workers must stop before any shared allocation is freed.
        if ready is not None:
            ready.close()
            ready.join_thread()

    def _start(self):
        if self.pool is not None:
            return
        context = mp.get_context('spawn')
        self.ready = context.Queue()
        self.pool = context.Pool(self.workers, initializer=self.initializer,
                                 initargs=(self.ready,), maxtasksperchild=32)
        deadline, pids = time.monotonic() + self.timeout, set()
        while len(pids) < self.workers:
            pid, error = self.ready.get(timeout=max(0.001, deadline - time.monotonic()))
            if error:
                raise RuntimeError('PDF worker initialization: ' + error)
            pids.add(pid)

    def warmup(self):
        with self.lock:
            if self.closed or self.disabled or self.workers < 2:
                return False
            if self.memory() < RESERVE + self.workers * 384 * MIB:
                return False
            try:
                self._start()
                return True
            except Exception as error:
                self._stop()
                self.disabled = str(error)
                return False
            except BaseException:
                self._stop()
                raise

    def _log(self):
        try:
            self.log('[pdf-cpu] ' + str(self.last_stats))
        except Exception:
            pass  # A broken diagnostic sink must not repeat a successful decode.

    def close(self):
        with self.lock:
            self.closed = True
            self._stop()

    def release(self):
        """End the PDF phase without permanently disabling future PDF requests."""
        with self.lock:
            self._stop()

    def _admit(self, data, shapes):
        if len(shapes) < 2 or len(shapes) > 128:
            self.last_stats['admission_reason'] = 'page count outside parallel limits'
            return False
        capacities = [int(height) * int(width) * 4 for height, width in shapes]
        if any(size <= 0 for size in capacities):
            raise ValueError('Invalid PDF allocation')
        total = sum(capacities)
        # Only one output per active worker remains shared. Completed pages are
        # copied to owned arrays and unlinked before admitting another page.
        memory, shared = self.memory(), self.shm_capacity()
        self.last_stats.update(available_memory_bytes=memory, available_shared_bytes=shared)
        for slots in range(min(self.workers, len(shapes)), 1, -1):
            shared_outputs = sum(sorted(capacities, reverse=True)[:slots])
            # All pool interpreters remain budgeted even when fewer are active.
            reserve = (total + shared_outputs + slots * max(capacities)
                       + self.workers * 384 * MIB + (slots + 2) * len(data) + 256 * MIB)
            reason = ('small request' if total < 64 * MIB else
                      'request memory budget' if reserve > self.budget else
                      'available memory reserve' if reserve > memory - RESERVE else
                      'shared memory capacity' if shared_outputs + len(data) + 64 * MIB > shared else
                      'admitted')
            self.last_stats.update(reservation_bytes=reserve, shared_bytes=shared_outputs + len(data),
                                   window_pages=slots, admission_reason=reason)
            if reason == 'admitted':
                return True
        return False

    def _parallel(self, data, shapes):
        segments, jobs = {}, {}

        def release(segment):
            segments.pop(segment.name)
            try:
                segment.close()
            finally:
                segment.unlink()

        try:
            source = shared_memory.SharedMemory(create=True, size=len(data))
            segments[source.name] = source
            source.buf[:len(data)] = data
            self._start()
            # Drain initializer reports from recycled workers; bound queue growth.
            while True:
                try:
                    _, error = self.ready.get_nowait()
                    if error:
                        raise RuntimeError('Recycled PDF worker initialization: ' + error)
                except queue.Empty:
                    break
            images = [None] * len(shapes)
            cursor, completed = 0, 0
            window = self.last_stats.get('window_pages', min(self.workers, len(shapes)))
            deadline = time.monotonic() + self.timeout
            while completed < len(shapes):
                if self.memory() < RESERVE:
                    raise MemoryError('Low memory during PDF preparation')
                if time.monotonic() >= deadline:
                    raise TimeoutError('PDF preparation timed out')
                while cursor < len(shapes) and len(jobs) < window:
                    height, width = shapes[cursor]
                    output = shared_memory.SharedMemory(create=True, size=int(height) * int(width) * 4)
                    segments[output.name] = output
                    task = (cursor, source.name, len(data), output.name, shapes[cursor])
                    job = self.pool.apply_async(self.render, (task,))
                    jobs[cursor] = (job, output)
                    cursor += 1
                    self.last_stats['peak_pending_pages'] = max(
                        self.last_stats.get('peak_pending_pages', 0), len(jobs))
                progressed = False
                for index, (job, output) in list(jobs.items()):
                    if not job.ready():
                        continue
                    page, shape = job.get()
                    if (page != index or len(shape) != 3 or tuple(shape[:2]) != tuple(shapes[index])
                            or shape[2] not in (3, 4)):
                        raise ValueError('Invalid PDF worker result')
                    if self.memory() < RESERVE:
                        raise MemoryError('Low memory copying PDF results')
                    images[index] = np.ndarray(shape, dtype=np.uint8, buffer=output.buf).copy()
                    release(output)
                    del jobs[index]
                    completed += 1
                    progressed = True
                if not progressed:
                    time.sleep(0.02)
            return images, self.make_info(images)
        except BaseException:
            self._stop()
            raise
        finally:
            for segment in list(segments.values()):
                try:
                    segment.close()
                finally:
                    segment.unlink()

    def __call__(self, data, max_num_imgs=None):
        with self.lock:
            began = time.perf_counter()
            self.last_stats = dict(parallel=False, workers=self.workers, fallback=None)
            if not self.closed and not self.disabled and self.workers >= 2:
                try:
                    shapes = self.plan(data, max_num_imgs)
                    if self._admit(data, shapes):
                        result = self._parallel(data, shapes)
                        self.last_stats.update(parallel=True, pages=len(result[0]),
                                               seconds=time.perf_counter() - began)
                        self._log()
                        return result
                except Exception as error:
                    self._stop()
                    self.disabled = str(error)
                    self.last_stats['fallback'] = str(error)
                except BaseException:
                    self._stop()
                    raise
            result = self.serial(data, max_num_imgs=max_num_imgs)
            self.last_stats.update(pages=len(result[0]), seconds=time.perf_counter() - began,
                                   disabled=self.disabled)
            self._log()
            return result
