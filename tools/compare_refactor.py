"""Compare an explicit pre-refactor module snapshot with current real-page output.

Uses temporary artifacts, never source/output caches. OCR is mocked identically
on both sides; this checks layout, character placement, pixels and cache identity,
not absolute recognition accuracy or live-service throughput.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_to_searchable_pdf as current


def compare(args):
    spec = importlib.util.spec_from_file_location('baseline_ocr', args.baseline)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    layouts = json.loads(args.layout.read_text(encoding='utf-8'))
    numbers = [int(n) for n in args.pages.split(',')]
    chosen = [layouts[n-1] if isinstance(layouts, list) else layouts['pages'][str(n-1)]['result']
              for n in numbers]
    args.out.mkdir(parents=True, exist_ok=True)
    subset = args.out/'source.pdf'
    with fitz.open(args.source) as doc:
        doc.select([n-1 for n in numbers])
        doc.save(subset)
    decisions, reports = [], []
    for label, module in (('before', baseline), ('after', current)):
        refiner = module.LineRefiner(args.out/f'{label}-lines.json', set(range(1, len(chosen)+1)), automatic=True)
        try:
            with patch.object(module, 'recognize_lines', side_effect=lambda images, **kw: ['?']*len(images)):
                reports.append(module.overlay(subset, chosen, args.out/f'{label}.pdf',
                                               line_refiner=refiner, debug=True, automatic=True))
            decisions.append(refiner.cache['decisions'])
        finally:
            refiner.close()
    # Wall-time / scheduling telemetry is not a layout or accuracy decision.
    comparable = [{k: v for k, v in report.items() if k != 'prefetch_stats'} for report in reports]
    assert comparable[0] == comparable[1], 'Different layout reports'
    assert decisions[0] == decisions[1], 'Different image keys, selection, or alignment decisions'
    checks = []
    with fitz.open(args.out/'before.pdf') as old, fitz.open(args.out/'after.pdf') as new:
        for index in range(len(old)):
            assert old[index].get_text('rawdict') == new[index].get_text('rawdict'), 'Different text or glyph coordinates'
            a, b = old[index].get_pixmap(), new[index].get_pixmap()
            assert a.irect == b.irect and a.samples == b.samples, 'Different rendered pixels'
            preview = new[index].get_pixmap(matrix=fitz.Matrix(1500/new[index].rect.height,
                                                              1500/new[index].rect.height))
            preview.save(args.out/f'page-{numbers[index]}.png')
            checks.append({'page': numbers[index], 'text_coordinates_pixels_equal': True})
    result = {'pages': checks, 'decisions': len(decisions[0]),
              'cache_keys_and_reports_equal': True, 'ocr': 'identical mocked response on both sides'}
    current.atomic_json(args.out/'comparison.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source', type=Path)
    ap.add_argument('layout', type=Path)
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--pages', default='6,7,166')
    ap.add_argument('--out', type=Path, required=True)
    compare(ap.parse_args())
