"""Exercise automatic edge recovery on real cached pages, without manual reviews."""
import argparse
import json
import sys
from pathlib import Path
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr
import ocr_boundary
import ocr_workflow

ap = argparse.ArgumentParser()
ap.add_argument('source', type=Path)
ap.add_argument('cache', type=Path)
ap.add_argument('--pages', default='6,7')
ap.add_argument('--out', type=Path, default=Path('output/pdf/automatic_boundary'))
args = ap.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
indices = [int(n)-1 for n in args.pages.split(',')]
pages = json.loads(args.cache.read_text(encoding='utf-8'))
subset = args.out/'source.pdf'
with fitz.open(args.source) as doc:
    doc.select(indices)
    doc.save(subset)
fixed = ocr_boundary.repair(subset, [pages[n] for n in indices],
                           args.out/'boundary_cache.json', ocr.source_identity(subset))
ocr.atomic_json(args.out/'automatic_layout.json', fixed)
for debug in (False, True):
    out = args.out/('debug.pdf' if debug else 'searchable.pdf')
    refiner = ocr.LineRefiner(args.out/'lines.json', set(range(1,len(fixed)+1)), automatic=True)
    try:
        report = ocr.overlay(subset, fixed, out, debug=debug, automatic=True, line_refiner=refiner)
    finally:
        refiner.close()
    ocr_workflow.verify(subset, out, report, debug=debug)
    ocr.atomic_json(args.out/('debug_report.json' if debug else 'report.json'), report)
    if report['validation_failed']:
        raise RuntimeError('PDF validation failed')
with fitz.open(args.out/'searchable.pdf') as doc:
    for page in doc:
        print('CHAPTER hits:', len(page.search_for('CHAPTER')), flush=True)
    if args.pages == '6,7':
        for pi, chapters in enumerate((('01','02'),('03','04'))):
            for chapter in chapters:
                assert doc[pi].search_for('CHAPTER '+chapter), f'CHAPTER {chapter} missing'
