"""Bounded document HTTP overlap; PDF iteration and checkpointing stay on owner.

Keep the caller's original PDF chunks, batch boundaries and recognition options.
Only immutable bytes cross threads. Yield in input order, never completion order.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import time
import requests as http


def document_results(chunks, batch, completed, recognize, *, requests=2,
                     budget=256*1024**2, stats=None):
    """Yield (start, response, total), retaining at most two outstanding chunks.

    Budget is an admission estimate (4x raw bytes for transport), not total RSS.
    A single oversized chunk runs alone without resizing or changing batch size.
    On request failure, drain existing HTTP work before one serial retry, and
    remain serial for this invocation. Invalid response content is the caller's
    responsibility and must not be checkpointed or retried as a scheduling error.
    """
    if requests not in (1, 2) or budget < 1 or batch < 1:
        raise ValueError('Invalid document prefetch limits')
    stats = stats if stats is not None else {}
    stats.update(requests=0, peak_pending=0, peak_transport_bytes=0,
                 prepare_seconds=0.0, wait_seconds=0.0, budget_drains=0, fallbacks=[])
    jobs = deque()
    limit, expected, document_total = requests, 0, None
    iterator = iter(chunks)

    def finish():
        nonlocal limit
        start, chunk, total, future = jobs.popleft()
        began = time.perf_counter()
        try:
            try:
                result = future.result()
            except Exception as error:
                if isinstance(error, http.HTTPError):
                    status = getattr(error.response, 'status_code', None)
                    if status is not None and status not in (429, 500, 502, 503, 504):
                        raise  # Invalid/auth requests do not improve with retry.
                limit = 1
                stats['fallbacks'].append(str(error))
                print(f'[ocr-feed] serial fallback ({error})', flush=True)
                # Do not retry alongside a later client request still in flight.
                # Its success/error remains available for its own ordered turn.
                for _, _, _, other in jobs:
                    try:
                        other.result()
                    except Exception:
                        pass
                stats['requests'] += 1
                result = recognize(chunk)
            return start, result, total
        finally:
            stats['wait_seconds'] += time.perf_counter()-began

    with (ThreadPoolExecutor(max_workers=2, thread_name_prefix='ocr-document')
          if requests == 2 else nullcontext(None)) as executor:
        try:
            while True:
                began = time.perf_counter()
                try:
                    start, chunk, total = next(iterator)
                except StopIteration:
                    break
                finally:
                    stats['prepare_seconds'] += time.perf_counter()-began
                if completed > total or (completed != total and completed % batch):
                    raise RuntimeError('Incomplete batch in OCR checkpoint; refusing a shifted resume')
                if (start != expected or total < 1 or start >= total or
                        (document_total is not None and total != document_total)):
                    raise RuntimeError('Invalid OCR batch sequence; refusing shifted pages')
                document_total, expected = total, min(start+batch, total)
                if start < completed:
                    continue
                size = 4*len(chunk)
                while jobs and (limit == 1 or size+sum(4*len(job[1]) for job in jobs) > budget):
                    stats['budget_drains'] += 1
                    yield finish()
                if limit == 1:
                    stats['requests'] += 1
                    stats['peak_pending'] = max(stats['peak_pending'], 1)
                    stats['peak_transport_bytes'] = max(stats['peak_transport_bytes'], size)
                    yield start, recognize(chunk), total
                    continue
                future = executor.submit(recognize, chunk)
                jobs.append((start, chunk, total, future))
                stats['requests'] += 1
                stats['peak_pending'] = max(stats['peak_pending'], len(jobs))
                stats['peak_transport_bytes'] = max(stats['peak_transport_bytes'],
                                                   sum(4*len(job[1]) for job in jobs))
                if len(jobs) >= limit or size > budget:
                    yield finish()
            while jobs:
                yield finish()
            if document_total is not None and expected != document_total:
                raise RuntimeError('Incomplete OCR batch sequence')
        finally:
            for _, _, _, future in jobs:
                future.cancel()
            # Closing a split-PDF generator also closes its owner-thread PDF.
            close = getattr(iterator, 'close', None)
            if close is not None:
                close()
