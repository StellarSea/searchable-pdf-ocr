"""Compare exact real-page line boxes before, cold-cache, and warm-cache.

Reads source/layout only; uses a fresh diagnostic cache under --out.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_render as current
from ocr_schema import _find_blocks, _find_size
from ocr_storage import LineCache, atomic_json


def run(args):
    spec = importlib.util.spec_from_file_location('old_render', args.baseline)
    before = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(before)
    layouts = json.loads(args.layout.read_text(encoding='utf-8'))
    numbers = [int(n) for n in args.pages.split(',')]
    args.out.mkdir(parents=True, exist_ok=True)
    cache_path = args.out/'detections.json'
    if cache_path.with_suffix('.sqlite3').exists():
        raise RuntimeError('Use a fresh output directory for a cold-cache benchmark')
    with args.source.open('rb') as stream:
        source_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    timings, results, stats = {}, {}, {}
    for phase in ('before', 'cold', 'warm'):
        store = LineCache(cache_path, True)
        boxes = []
        try:
            with fitz.open(args.source) as doc:
                start = time.perf_counter()
                for index, number in enumerate(numbers):
                    # Source can be the original book or the explicit subset.
                    page = doc[index if args.subset else number-1]
                    pruned = layouts[number-1] if isinstance(layouts, list) else layouts['pages'][str(number-1)]['result']
                    width, height = _find_size(pruned)
                    sx, sy = page.rect.width/width, page.rect.height/height
                    raster = current.PageRaster(page, source_sha, store)
                    for text, bbox in _find_blocks(pruned):
                        rect = fitz.Rect(bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy) & page.rect
                        if phase == 'before':
                            detected = before.detect_lines(page, rect)
                        elif phase == 'warm':
                            with patch.object(raster, 'get_pixmap', side_effect=AssertionError('warm cache rendered')):
                                detected = current.detect_lines(page, rect, raster)
                        else:
                            detected = current.detect_lines(page, rect, raster)
                        boxes.append([list(r) for r in detected])
                timings[phase] = time.perf_counter()-start
            results[phase] = boxes
            stats[phase] = dict(store.detection_stats)
        finally:
            store.close()
    assert results['before'] == results['cold'] == results['warm'], 'Changed line coordinates'
    report = {'pages': numbers, 'blocks': len(results['before']),
              'lines': sum(map(len, results['before'])), 'exact_boxes': True,
              'seconds': timings, 'cache_stats': stats}
    atomic_json(args.out/'comparison.json', report)
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('layout', type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--pages', default='6,7,166')
    parser.add_argument('--subset', action='store_true')
    parser.add_argument('--out', required=True, type=Path)
    run(parser.parse_args())
