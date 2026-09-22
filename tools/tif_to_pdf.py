"""Build one PDF from scanned TIFF pages, ready for run.py.

Pages are written with no rotation and no crop box, so needs_flattening() in
compose/ocr_to_searchable_pdf.py leaves the scan at its own resolution instead
of re-rendering every page at OCR_RENDER_ZOOM.
"""
import argparse
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import io
import math
import multiprocessing
import os
from pathlib import Path
import time
import warnings

import pymupdf
import psutil
from PIL import Image, ImageSequence

def positive_dpi(value):
    dpi = float(value)
    if not math.isfinite(dpi) or dpi <= 0:
        raise ValueError('DPI must be a finite positive number')
    return dpi


def read_dpi(frame, override=None):
    """Read both axes from this TIFF frame before conversion drops its tags.

    Pillow may synthesize (1, 1) for TIFFs with no resolution tags. Inspect the
    actual tags instead: 282/283 are X/YResolution, 296 is ResolutionUnit.
    TIFF's omitted ResolutionUnit defaults to inches; unit 1 is only a ratio.
    """
    if override is not None:
        dpi = positive_dpi(override)
        return dpi, dpi
    tags = frame.tag_v2
    try:
        unit = int(tags.get(296, 2))
        if unit not in (2, 3):
            raise ValueError('resolution has no physical unit')
        factor = 2.54 if unit == 3 else 1.0
        return (positive_dpi(float(tags[282]) * factor),
                positive_dpi(float(tags[283]) * factor))
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        warnings.warn(
            f'{getattr(frame, "filename", "TIFF")} frame {frame.tell()+1}: '
            'missing or invalid physical DPI; assuming 600 x 600 DPI. '
            'Use --dpi to specify the scan resolution.', stacklevel=2)
        return 600.0, 600.0


def insert_lossless_image(doc, page, image):
    """Compress each RGB/gray image now instead of retaining a whole book raw."""
    space = pymupdf.csGRAY if image.mode == 'L' else pymupdf.csRGB
    samples = image.tobytes()
    pixmap = pymupdf.Pixmap(space, image.width, image.height, samples, False)
    xref = page.insert_image(page.rect, pixmap=pixmap, keep_proportion=False)
    # MuPDF may pack black/white gray pixels into 1 bit. Compress its encoded
    # samples so the stream still matches BitsPerComponent / ColorSpace.
    doc.update_stream(xref, doc.xref_stream(xref), compress=True)
    return xref


def append_frame(doc, frame, dpi, quality, lossless):
    """One process owns this frame and its PDF; never share PDF objects."""
    xdpi, ydpi = read_dpi(frame, dpi)
    image = frame if frame.mode in ('L', 'RGB') else frame.convert('RGB')
    page = doc.new_page(width=image.width / xdpi * 72,
                        height=image.height / ydpi * 72)
    if lossless:
        insert_lossless_image(doc, page, image)
    else:
        buffer = io.BytesIO()
        image.save(buffer, 'JPEG', quality=quality, dpi=(xdpi, ydpi))
        page.insert_image(page.rect, stream=buffer.getvalue(), keep_proportion=False)


def plan_frames(sources):
    """Read headers only, retaining file/frame order and a memory estimate."""
    jobs = []
    for path in sources:
        with Image.open(path) as tiff:
            for frame in ImageSequence.Iterator(tiff):
                # Allow for decoded TIFF, conversion, pixmaps, stream copies,
                # compression and IPC. This is a budget, not an OS memory cap.
                channels = max(3, len(frame.getbands()))
                reserve = frame.width * frame.height * channels * 8 + 128 * 1024**2
                jobs.append((str(path), frame.tell(), reserve))
    return jobs


def choose_workers(jobs, workers, memory_mb, *, available=None, cpu_count=None):
    if workers < 0 or memory_mb <= 0:
        raise ValueError('workers must be >= 0 and memory_mb must be > 0')
    cores = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    if os.name == 'nt':
        cores = min(cores, 61)  # ProcessPoolExecutor's Windows limit.
    available = psutil.virtual_memory().available if available is None else available
    budget = min(memory_mb * 1024**2, max(0, available - 1024**3) // 2)
    largest = max((job[2] for job in jobs), default=1)
    return max(1, min(workers or 4, cores, len(jobs), budget // largest))


def prepare_frame(job, dpi, quality, lossless):
    """Spawn worker returns one compressed page, never uncompressed pixels."""
    path, frame_number, _ = job
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with Image.open(path) as tiff, pymupdf.open() as doc:
            tiff.seek(frame_number)
            append_frame(doc, tiff, dpi, quality, lossless)
            data = doc.tobytes(deflate=True, garbage=3)
    return data, [str(item.message) for item in caught]


def prepared_frames(jobs, workers, dpi, quality, lossless, *,
                    executor_factory=ProcessPoolExecutor, prepare=prepare_frame):
    """Bound in-flight and completed results to workers; consume source order."""
    next_page = 0
    pending = deque()
    pool = None
    try:
        pool = executor_factory(max_workers=workers,
                                mp_context=multiprocessing.get_context('spawn'))
        submitted = 0
        while next_page < len(jobs):
            while submitted < len(jobs) and len(pending) < workers:
                pending.append(pool.submit(prepare, jobs[submitted], dpi, quality, lossless))
                submitted += 1
            future = pending[0]
            while True:
                try:
                    result = future.result(timeout=5)
                    break
                except TimeoutError:
                    if future.done():
                        result = future.result()  # Also propagate task-raised errors.
                        break
                    print(f'\r페이지 {next_page + 1}/{len(jobs)} 압축 대기 중... ',
                          end='', flush=True)
            pending.popleft()
            next_page += 1
            yield result
            del result, future
    except (BrokenProcessPool, OSError) as error:
        warnings.warn(f'TIFF worker pool unavailable; continuing sequentially '
                      f'from page {next_page + 1}: {error}', stacklevel=2)
        for future in pending:
            future.cancel()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
            pool = None
        pending.clear()
        # Pages already yielded are retained; retry only the unconsumed tail.
        for job in jobs[next_page:]:
            yield prepare(job, dpi, quality, lossless)
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


def convert_tiffs(sources, output, *, dpi=None, quality=88, lossless=False,
                  workers=1, memory_mb=4096):
    """Keep source pixel dimensions and use each frame's physical resolution."""
    if dpi is not None:
        positive_dpi(dpi)
    if workers < 0 or memory_mb <= 0:
        raise ValueError('workers must be >= 0 and memory_mb must be > 0')
    started = time.monotonic()
    pages = 0
    jobs = plan_frames(sources) if workers != 1 else []
    count = choose_workers(jobs, workers, memory_mb) if jobs else 1
    phase = '무손실 압축 완료' if lossless else 'JPEG 변환 완료'
    print(f'TIFF 변환: {count}개 프로세스'
          f' (작업자 메모리 예산 {memory_mb} MiB)', flush=True)
    with pymupdf.open() as doc:
        if count > 1:
            for data, messages in prepared_frames(jobs, count, dpi, quality, lossless):
                for message in messages:
                    warnings.warn(message, stacklevel=2)
                with pymupdf.open(stream=data, filetype='pdf') as part:
                    doc.insert_pdf(part)
                pages += 1
                print(f'\r{pages}/{len(jobs)}페이지 {phase}'
                      f' ({time.monotonic() - started:.0f}초)', end='', flush=True)
                del data
        else:
            for done, path in enumerate(sources, 1):
                with Image.open(path) as tiff:
                    for frame in ImageSequence.Iterator(tiff):
                        append_frame(doc, frame, dpi, quality, lossless)
                        pages += 1
                        print(f'\r파일 {done}/{len(sources)}, {pages}페이지 {phase}'
                              f' ({time.monotonic() - started:.0f}초)', end='', flush=True)
        print(f'\nPDF 저장 중: {pages}페이지 → {output}', flush=True)
        doc.save(output, deflate=True, garbage=3)
        print(f'PDF 저장 완료 ({time.monotonic() - started:.1f}초)', flush=True)
        return len(doc)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Convert scanned TIFF pages into one PDF')
    parser.add_argument('source', type=Path, help='folder of TIFF pages, or a single TIFF')
    parser.add_argument('output', type=Path, help='PDF to write')
    parser.add_argument('--quality', type=int, default=88, help='JPEG quality (default: 88)')
    parser.add_argument('--lossless', action='store_true',
                        help='store the pixels with Flate instead of JPEG')
    parser.add_argument('--dpi', type=float, help='override both TIFF DPI axes')
    parser.add_argument('--workers', type=int, default=0,
                        help='compression processes: 0=auto up to 4 (default), 1=serial')
    parser.add_argument('--memory-mb', type=int, default=4096,
                        help='estimated worker memory budget in MiB (default: 4096)')
    args = parser.parse_args(argv)
    if args.workers < 0 or args.memory_mb <= 0:
        parser.error('--workers must be >= 0 and --memory-mb must be > 0')
    if args.dpi is not None:
        try:
            positive_dpi(args.dpi)
        except ValueError as error:
            parser.error(str(error))
    if args.source.is_dir():
        sources = sorted(p for p in args.source.iterdir()
                         if p.suffix.lower() in ('.tif', '.tiff'))
    else:
        sources = [args.source]
    if not sources:
        parser.error(f'No TIFF pages in {args.source}')
    pages = convert_tiffs(sources, args.output, dpi=args.dpi, quality=args.quality,
                          lossless=args.lossless, workers=args.workers,
                          memory_mb=args.memory_mb)
    print('저장된 PDF 페이지·회전·크롭 확인 중...', flush=True)
    # The pipeline only keeps full resolution while both of these hold.
    with pymupdf.open(args.output) as written:
        upright = all(page.rotation == 0 and tuple(page.cropbox) == tuple(page.mediabox)
                      for page in written)
    size = args.output.stat().st_size / 1024 ** 2
    print(f'\r{pages} 페이지, {size:.0f} MB → {args.output}')
    print('회전·크롭 없음: OCR이 원본 해상도로 읽습니다' if upright else
          '경고: 회전이나 크롭 박스가 남아 OCR 직전에 다운샘플됩니다')


if __name__ == '__main__':
    main()
