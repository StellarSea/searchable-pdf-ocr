"""Container-only experiment: exact PDFium rendering via separate processes.

No server mutation or predictor construction. Includes array IPC/copy cost and
process startup; compares every output byte to the installed serial renderer.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import gc
import hashlib
import json
import multiprocessing as mp
from multiprocessing import shared_memory
from pathlib import Path
import time

import numpy as np


_pdf_bytes = None


def initialize(data):
    global _pdf_bytes
    _pdf_bytes = data


def render(index):
    import pypdfium2 as pdfium
    from paddlex.inference.serving.infra.utils import (
        PDF_RENDER_SCALE, PDF_MIN_RENDER_SCALE, MAX_IMAGE_PIXELS,
        render_pdf_page_to_numpy, ensure_image_pixel_limit,
    )
    with pdfium.PdfDocument(_pdf_bytes) as doc:
        doc.init_forms()
        page = doc[index]
        try:
            image = render_pdf_page_to_numpy(page, page_index=index + 1,
                requested_scale=PDF_RENDER_SCALE, rotation=0,
                min_scale=PDF_MIN_RENDER_SCALE, max_pixels=MAX_IMAGE_PIXELS)
            ensure_image_pixel_limit(image, page_index=index + 1)
            return image
        finally:
            page.close()


def signature(images):
    return [(list(image.shape), str(image.dtype), hashlib.sha256(np.ascontiguousarray(image)).hexdigest())
            for image in images]


def render_shared(task):
    index, name, shape, dtype = task
    image = render(index)
    if list(image.shape) != shape or str(image.dtype) != dtype:
        raise ValueError('Shared output allocation shape does not match actual PDF pixels')
    segment = shared_memory.SharedMemory(name=name)
    try:
        view = np.ndarray(shape, dtype=dtype, buffer=segment.buf)
        np.copyto(view, image)
        del view
    finally:
        segment.close()
    return index


def shared_images(pool, reference):
    """Owner allocates and unlinks; workers only attach, fill and close.

    Return ordinary owned NumPy arrays. Include this final copy in timings;
    never expose shared-memory views to the rest of the OCR pipeline.
    """
    segments, tasks = [], []
    try:
        for index, (shape, dtype, _) in enumerate(reference):
            size = int(np.prod(shape)) * np.dtype(dtype).itemsize
            segment = shared_memory.SharedMemory(create=True, size=size)
            segments.append(segment)
            tasks.append((index, segment.name, shape, dtype))
        # Materialize every future so no task can attach after cleanup.
        futures = []
        try:
            for task in tasks:
                futures.append(pool.submit(render_shared, task))
            if [future.result() for future in futures] != list(range(len(tasks))):
                raise ValueError('Shared PDF page ordering mismatch')
            return [np.ndarray(shape, dtype=dtype, buffer=segment.buf).copy()
                    for segment, (shape, dtype, _) in zip(segments, reference)]
        finally:
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass
    finally:
        for segment in segments:
            try:
                segment.close()
            finally:
                segment.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--transfer', choices=('pickle', 'shared'), default='pickle')
    args = parser.parse_args()
    from paddlex.inference.serving.infra.utils import file_to_images
    args.out.mkdir(parents=True, exist_ok=False)
    data = args.pdf.read_bytes()
    reference, count, records = None, None, []
    for workers in ((0, 2, 4, 4, 2, 0) if args.transfer == 'pickle' else (0, 4, 4, 0)):
        for cold in (True, False):
            start = time.perf_counter()
            if workers:
                if cold:
                    pool = ProcessPoolExecutor(max_workers=workers,
                        mp_context=mp.get_context('spawn'), initializer=initialize, initargs=(data,))
                images = (shared_images(pool, reference) if args.transfer == 'shared'
                          else list(pool.map(render, range(count))))
            else:
                images, _ = file_to_images(data, 'PDF', max_num_imgs=None)
            seconds = time.perf_counter() - start
            current = signature(images)
            if reference is None:
                reference, count = current, len(images)
            if current != reference:
                raise AssertionError('Parallel PDFium pixels differ')
            total_bytes = sum(image.nbytes for image in images)
            del images
            gc.collect()
            record = dict(workers=workers, cold=cold, seconds=seconds, exact=True)
            records.append(record)
            (args.out / 'comparison.json').write_text(json.dumps(dict(
                input_sha256=hashlib.sha256(data).hexdigest(), pages=count, transfer=args.transfer,
                decoded_bytes=total_bytes, signatures=reference, records=records), indent=2))
            print(f'[pdf-probe] workers={workers}, cold={cold}: {seconds:.3f}s, exact', flush=True)
        if workers:
            pool.shutdown()


if __name__ == '__main__':
    main()
