"""Bounded, cache-first GPU feed from already prepared CPU page results.

Only HTTP bytes enter the existing single HTTP executor. All cache reads/writes
stay on the owner. Speculation never publishes layout decisions or restarts a
service; failures revert to the original per-block recognition path.
"""
import hashlib
import threading
import time

from ocr_layout import line_settings, needs_crops
from ocr_schema import _find_blocks
from ocr_text import line_language


class GPUPrefetch:
    def __init__(self, store, executor, recognize, layouts, pages, service=None,
                 max_images=64, max_requests=2, budget=64*1024**2):
        if not 1 <= max_images <= 256 or max_requests < 1 or budget < 1:
            raise ValueError('Invalid GPU prefetch limits')
        self.store, self.executor, self.recognize = store, executor, recognize
        self.layouts, self.pages, self.service = layouts, pages, service
        self.max_images, self.max_requests, self.budget = max_images, max_requests, budget
        self.jobs, self.pending, self.seen_pages = [], set(), set()
        self.owner = threading.get_ident()
        self.disabled = False
        self.stats = {'requests': 0, 'images': 0, 'cache_hits': 0, 'oversize_skips': 0,
                      'peak_requests': 0, 'peak_buffer_bytes': 0, 'wait_seconds': 0.0,
                      'errors': [], 'completed_requests': 0}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()

    def check_owner(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError('GPU prefetch cache access requires the owner thread')

    def stop(self):
        self.disabled = True
        for keys, future, size in self.jobs:
            future.cancel()  # Running HTTP is joined by its executor's owner.
        self.jobs.clear()
        self.pending.clear()

    def fail(self, error):
        self.stats['errors'].append(str(error))
        print(f'[gpu-feed] per-block fallback ({error})', flush=True)
        self.stop()

    def harvest(self):
        self.check_owner()
        if self.disabled:
            return
        try:
            for job in list(self.jobs):
                keys, future, size = job
                if not future.done():
                    continue
                values = future.result()
                if (not isinstance(values, list) or len(values) != len(keys)
                        or not all(isinstance(value, str) for value in values)):
                    raise ValueError('GPU prefetch returned invalid results')
                self.store.save_responses(dict(zip(keys, values)))
                self.pending.difference_update(keys)
                self.jobs.remove(job)
                self.stats['completed_requests'] += 1
        except Exception as error:
            self.fail(error)

    def entries(self, pno, blocks):
        if pno+1 not in self.pages:
            return
        source = _find_blocks(self.layouts[pno])
        for index, (boxes, chunks, images) in blocks.items():
            text = source[index][0].replace('\ufffd', '')
            if not needs_crops(text, boxes):
                continue
            settings, lang = line_settings(text), line_language(text)
            for png in images.values():
                yield lang, hashlib.sha256(settings.encode()+png).hexdigest(), png

    def offer(self, prepared):
        """Accept (page_number, prepared_blocks) pairs, without retaining pages."""
        self.check_owner()
        self.harvest()
        if self.disabled or len(self.jobs) >= self.max_requests:
            return
        buffers, buffered_keys = {}, set()
        retained = sum(job[2] for job in self.jobs)

        def flush(lang):
            entries = buffers.pop(lang)
            if self.service:
                self.service.ensure()
            keys = [key for key, png in entries]
            future = self.executor.submit(self.recognize, [png for key, png in entries], lang=lang)
            size = 4*sum(len(png) for key, png in entries)
            self.jobs.append((keys, future, size))
            self.pending.update(keys)
            self.stats['requests'] += 1
            self.stats['images'] += len(keys)
            self.stats['peak_requests'] = max(self.stats['peak_requests'], len(self.jobs))
            self.stats['peak_buffer_bytes'] = max(self.stats['peak_buffer_bytes'], retained)

        try:
            full = False
            for pno, blocks in prepared:
                if pno in self.seen_pages:
                    continue
                for lang, key, png in self.entries(pno, blocks):
                    if key in self.pending or key in buffered_keys:
                        continue
                    if self.store.response(key) is not None:
                        self.stats['cache_hits'] += 1
                        continue
                    size = 4*len(png)  # PNG + base64/JSON/transport headroom.
                    if size > self.budget:
                        self.stats['oversize_skips'] += 1
                        continue
                    if (retained+size > self.budget or
                            (lang not in buffers and len(self.jobs)+len(buffers) >= self.max_requests)):
                        full = True
                        break
                    buffers.setdefault(lang, []).append((key, png))
                    buffered_keys.add(key)
                    retained += size
                    if len(buffers[lang]) == self.max_images:
                        flush(lang)
                if full:
                    break
                self.seen_pages.add(pno)
            for lang in list(buffers):
                flush(lang)
        except Exception as error:
            self.fail(error)

    def wait(self, keys, on_idle=None):
        """Wait only for requested in-flight keys; never delay unrelated blocks."""
        self.check_owner()
        started = time.perf_counter()
        try:
            wanted = set(keys)
            while not self.disabled and wanted & self.pending:
                self.harvest()
                if self.disabled or not wanted & self.pending:
                    break
                if on_idle:
                    on_idle()
                job = next((job for job in self.jobs if wanted.intersection(job[0])), None)
                if job is None:
                    break
                try:
                    job[1].result(timeout=.1)
                except TimeoutError:
                    pass
                except Exception as error:
                    self.fail(error)
        finally:
            self.stats['wait_seconds'] += time.perf_counter()-started
