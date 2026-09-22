"""Build real, unchanged line crops from an existing overlay decision report."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source', type=Path)
    ap.add_argument('report', type=Path)
    ap.add_argument('out', type=Path)
    ap.add_argument('--exclude-pages', default='', help='Comma-separated pages reserved for another corpus')
    args = ap.parse_args()
    decisions = json.loads(args.report.read_text(encoding='utf-8'))['line_decisions']
    excluded = {int(n) for n in args.exclude_pages.split(',') if n}
    selected = []
    for lang, count in (('korean', 8), ('default', 4)):
        pool = [d for d in decisions if ('korean' in d['engine']) == (lang == 'korean')
                and d['page'] not in excluded
                and 2 <= len(d['rects']) <= 20
                and all(4 <= fitz.Rect(r).height <= 120 for r in d['rects'])]
        indices = sorted({round(i*(len(pool)-1)/(min(count, len(pool))-1))
                          for i in range(min(count, len(pool)))}) if len(pool) > 1 else [0]
        selected.extend((lang, pool[i]) for i in indices if pool)
    args.out.mkdir(parents=True, exist_ok=True)
    cases = []
    with fitz.open(args.source) as doc:
        for index, (lang, decision) in enumerate(selected):
            page = doc[decision['page']-1]
            raster = ocr.PageRaster(page)
            padding = ocr.page_coordinate_scale(page)
            files, hashes = [], []
            for line, coords in enumerate(decision['rects']):
                rect = fitz.Rect(coords)
                crop = fitz.Rect(rect.x0-padding, rect.y0-.7*padding,
                                 rect.x1+padding, rect.y1+.7*padding) & page.rect
                png = raster.get_pixmap(matrix=fitz.Matrix(ocr.ZOOM, ocr.ZOOM),
                                        clip=crop, colorspace=fitz.csRGB, alpha=False).tobytes('png')
                filename = f'block{index:02d}_line{line:02d}.png'
                (args.out/filename).write_bytes(png)
                files.append(filename)
                hashes.append(hashlib.sha256(png).hexdigest())
            cases.append({'id': f'block{index:02d}', 'lang': lang, 'page': decision['page'],
                          'files': files, 'sha256': hashes, 'kind': 'block',
                          'original': decision['original']})
    # Larger batches exercise headroom for future client-side batching. Keep
    # them separate from actual current per-block workloads in the report.
    for lang in ('korean', 'default'):
        files = [name for c in cases if c['lang'] == lang and c['kind'] == 'block' for name in c['files']]
        cases.append({'id': f'batch-{lang}', 'lang': lang, 'kind': 'batch', 'files': files[:64]})
    ocr.atomic_json(args.out/'corpus.json', {'cases': cases, 'source': str(args.source),
                                          'render_zoom': ocr.ZOOM})
    print(json.dumps({'blocks': len(selected), 'lines': sum(len(c['files']) for c in cases if c['kind']=='block'),
                      'pages': [c['page'] for c in cases if c['kind']=='block']}))


if __name__ == '__main__':
    main()
