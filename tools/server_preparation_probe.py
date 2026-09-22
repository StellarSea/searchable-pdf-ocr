"""Run INSIDE the existing PaddleX image: model-free CPU preparation A/B.

Reads a saved request PDF and its response. Never constructs a predictor or
calls HTTP. GPU layout results are replayed, not timed. Threads touch only
independent in-memory NumPy/PIL crops; PDFium conversion stays serial.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import gc
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import numpy as np


def digest(value):
    def canonical(item):
        if isinstance(item, np.ndarray):
            return {'shape': list(item.shape), 'dtype': str(item.dtype),
                    'sha256': hashlib.sha256(np.ascontiguousarray(item)).hexdigest()}
        if isinstance(item, np.generic):
            return item.item()
        if isinstance(item, dict):
            return {str(key): canonical(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [canonical(value) for value in item]
        if isinstance(item, set):
            return sorted(canonical(value) for value in item)
        return item
    return hashlib.sha256(json.dumps(canonical(value), ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--response', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    # Container-only imports are deliberately lazy. No model initialization.
    from paddlex.inference.serving.infra.utils import file_to_images
    from paddlex.inference.pipelines.paddleocr_vl.pipeline import _PaddleOCRVLPipeline
    from paddlex.inference.pipelines.components import CropByBoxes
    from paddlex.inference.pipelines.layout_parsing.utils import gather_imgs
    from paddlex.inference.models.doc_vlm.predictor import DocVLMGenAIClientPredictor

    args.out.mkdir(parents=True, exist_ok=False)
    data = args.pdf.read_bytes()
    rows = json.loads(args.response.read_text())['layoutParsingResults']
    began = time.perf_counter()
    images, _ = file_to_images(data, 'PDF', max_num_imgs=None)
    decode_seconds = time.perf_counter() - began
    if len(images) != len(rows):
        raise ValueError('PDF/response page count mismatch')
    for image, row in zip(images, rows):
        pruned = row['prunedResult']
        if image.shape[:2] != (pruned['height'], pruned['width']):
            raise ValueError('Server pixels do not match saved layout coordinates')
    layout = [row['prunedResult']['layout_det_res'] for row in rows]
    began = time.perf_counter()
    illustrations = [gather_imgs(image, result['boxes']) for image, result in zip(images, layout)]
    gather_seconds = time.perf_counter() - began
    pipe = object.__new__(_PaddleOCRVLPipeline)
    pipe.crop_by_boxes = CropByBoxes()
    config = dict(layout_shape_mode='auto', merge_layout_blocks=False,
                  image_labels=['image', 'header_image', 'footer_image', 'chart', 'seal'],
                  use_chart_recognition=False, use_seal_recognition=False)
    for label in ('ocr', 'table', 'chart', 'formula', 'seal'):
        config[label + '_min_pixels'] = 112896
        config[label + '_max_pixels'] = 1003520
    summary = {'input_sha256': hashlib.sha256(data).hexdigest(), 'pages': len(images),
               'page_shapes': [list(image.shape) for image in images],
               'decoded_bytes': sum(image.nbytes for image in images),
               'decode_seconds': decode_seconds, 'gather_seconds': gather_seconds,
               'layout': [], 'jpeg': [], 'scope': 'CPU stages only; cached GPU boxes; no inference'}

    def save():
        (args.out / 'comparison.json').write_text(json.dumps(summary, indent=2))

    print(f'[probe] decode={decode_seconds:.3f}s, gather={gather_seconds:.3f}s', flush=True)
    reference, reference_hash = None, None
    for workers in (0, 4, 8, 8, 4, 0):
        payloads = [(i, image, copy.deepcopy(layout[i]), copy.deepcopy(illustrations[i]), config)
                    for i, image in enumerate(images)]
        began = time.perf_counter()
        if workers:
            result = pipe._paddleocr_vl_layout_prep_parallel_pages(payloads, workers)
        else:
            result = [pipe._paddleocr_vl_prepare_page_serial_benchmarked(item) for item in payloads]
        seconds = time.perf_counter() - began
        result_hash = digest(result)
        if reference is None:
            reference, reference_hash = result, result_hash
        if result_hash != reference_hash:
            raise AssertionError('Parallel layout preparation changed pixels or metadata')
        summary['layout'].append(dict(workers=workers, seconds=seconds, sha256=result_hash))
        print(f'[probe] layout workers={workers}: {seconds:.3f}s, exact', flush=True)
        del result, payloads
        gc.collect()
        save()

    batches = pipe._paddleocr_vl_aggregate_vlm_batches(reference)[3]
    client = SimpleNamespace(backend='vllm-server')
    predictor = SimpleNamespace(model_name='PaddleOCR-VL-1.6-0.9B')
    build = DocVLMGenAIClientPredictor._doc_vlm_genai_build_request_specs
    # Preserve pixel-key grouping, query order, JPEG settings and request kwargs.
    groups = [(pixel_key, [dict(image=image, query=query)
               for image, query in zip(batch['images'], batch['queries'])])
              for pixel_key, batch in batches.items()]

    def build_group(group):
        pixel_key, items = group
        return build(predictor, client, items, 'JPEG', None, None, None, None,
                     None, pixel_key[0], pixel_key[1])

    specs_hash = None
    for workers in (0, 4, 8, 8, 4, 0):
        began = time.perf_counter()
        if workers:
            specs = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for pixel_key, items in groups:
                    parts = list(pool.map(build_group, [(pixel_key, [item]) for item in items]))
                    specs.extend(spec for part in parts for spec in part)
        else:
            specs = [spec for group in groups for spec in build_group(group)]
        seconds = time.perf_counter() - began
        current_hash = digest(specs)
        if specs_hash is None:
            specs_hash = current_hash
        if current_hash != specs_hash:
            raise AssertionError('JPEG bytes, prompts, order or generation parameters changed')
        summary['jpeg'].append(dict(workers=workers, seconds=seconds,
                                    requests=len(specs), sha256=current_hash))
        print(f'[probe] JPEG workers={workers}: {seconds:.3f}s, exact', flush=True)
        del specs
        gc.collect()
        save()
    summary['complete'] = True
    save()


if __name__ == '__main__':
    main()
