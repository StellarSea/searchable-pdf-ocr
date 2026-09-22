"""Verify the reusable server reader against native PDFium, without OCR.

--prepare-fixture runs on the host; the actual benchmark runs in PaddleX's image.
All writes go to a new explicit diagnostic directory. No service changes.
"""
import argparse
import gc
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
from ocr_server_pdf import ParallelPDFReader


def signatures(images):
    return [(list(image.shape), str(image.dtype), hashlib.sha256(np.ascontiguousarray(image)).hexdigest())
            for image in images]


def fixture(path):
    import pymupdf as fitz
    with fitz.open() as doc:
        for index in range(6):
            page = doc.new_page(width=1200 + index * 5, height=900 + index * 7)
            page.insert_text((80, 100), f'PDF CPU fixture {index}: 17, 1024', fontsize=28)
            page.draw_rect(fitz.Rect(120, 170, 850, 650), color=(1, 0, 0),
                           fill=(0, 0, 1), fill_opacity=0.35)
            page.add_text_annot((150, 130), f'Annotation {index}')
            widget = fitz.Widget()
            widget.field_name = f'field{index}'
            widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            widget.field_value = f'Form {index}'
            widget.rect = fitz.Rect(80, 700, 450, 750)
            page.add_widget(widget)
            if index % 2:
                page.set_cropbox(fitz.Rect(17, 23, 1180, 860))
            page.set_rotation((index % 4) * 90)
        doc.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--prepare-fixture', action='store_true')
    parser.add_argument('--pdf', type=Path)
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--stress', type=int, default=0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    if args.prepare_fixture:
        fixture(args.out / 'fixture.pdf')
        return
    if not args.pdf or not args.fixture or args.stress < 0:
        parser.error('Supply --pdf, --fixture and a nonnegative --stress')
    from paddlex.inference.serving.infra import utils
    original, synthetic = args.pdf.read_bytes(), args.fixture.read_bytes()
    reader = ParallelPDFReader(utils.read_pdf)
    began = time.perf_counter()
    warmed = reader.warmup()
    warm_seconds = time.perf_counter() - began
    records, baseline = [], {}
    cases = [('source', original, None), ('fixture', synthetic, None), ('source3', original, 3),
             ('source_again', original, None)]
    cases.extend(('stress', synthetic, None) for _ in range(args.stress))
    try:
        for label, data, limit in cases:
            key = (hashlib.sha256(data).hexdigest(), limit)
            if key not in baseline:
                began = time.perf_counter()
                images, info = utils.read_pdf(data, max_num_imgs=limit)
                serial_seconds = time.perf_counter() - began
                baseline[key] = (signatures(images), info.model_dump(), serial_seconds)
                del images
                gc.collect()
            began = time.perf_counter()
            images, info = reader(data, max_num_imgs=limit)
            seconds = time.perf_counter() - began
            expected, info_expected, serial_seconds = baseline[key]
            if signatures(images) != expected or info.model_dump() != info_expected:
                raise AssertionError('PDF pixels, shape, order or page info changed')
            records.append(dict(label=label, input_sha256=key[0], limit=limit, pages=len(images),
                serial_seconds=serial_seconds, seconds=seconds, stats=dict(reader.last_stats), exact=True))
            print(f'[reader] {label}: {seconds:.3f}s, exact; parallel={reader.last_stats["parallel"]}', flush=True)
            del images
            gc.collect()
            (args.out / 'comparison.json').write_text(json.dumps(dict(
                warmed=warmed, warmup_seconds=warm_seconds, records=records), indent=2))
    finally:
        reader.close()


if __name__ == '__main__':
    main()
