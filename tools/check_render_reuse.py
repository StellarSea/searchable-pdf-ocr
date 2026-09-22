"""Read-only real-document pixel/PNG/line-coordinate equivalence check."""
import argparse
import json
from pathlib import Path
import sys
import time

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('layout', type=Path)
    parser.add_argument('--pages', default='6,7,166')
    args = parser.parse_args()
    layouts = json.loads(args.layout.read_text(encoding='utf-8'))
    with fitz.open(args.source) as doc:
        for number in map(int, args.pages.split(',')):
            page, layout = doc[number-1], layouts[number-1]
            size = ocr._find_size(layout)
            _, bbox = max(ocr._find_blocks(layout), key=lambda block: len(block[0]))
            sx, sy = page.rect.width/size[0], page.rect.height/size[1]
            rect = fitz.Rect(bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy) & page.rect
            # Bound work while another overlay may be running. Include full width
            # and first heading / text rows; do not render a giant full page.
            rect.y1 = min(rect.y1, rect.y0+180)
            raster = ocr.PageRaster(page)
            elapsed = [0., 0.]
            for colorspace in (fitz.csGRAY, fitz.csRGB):
                kwargs = dict(matrix=fitz.Matrix(ocr.ZOOM, ocr.ZOOM), clip=rect,
                              colorspace=colorspace, alpha=False)
                pixmaps = []
                for i, render in enumerate((page, raster)):
                    start = time.perf_counter()
                    pixmaps.append(render.get_pixmap(**kwargs))
                    elapsed[i] += time.perf_counter()-start
                before, after = pixmaps
                assert before.irect == after.irect
                assert before.samples == after.samples, f'p{number}: pixels changed'
                assert before.tobytes('png') == after.tobytes('png'), f'p{number}: PNG changed'
            before_lines = ocr.detect_lines(page, rect)
            after_lines = ocr.detect_lines(page, rect, raster=raster)
            assert before_lines == after_lines, f'p{number}: line boxes changed'
            print(json.dumps({'page': number, 'pixels_png_boxes_equal': True,
                              'line_boxes': len(before_lines),
                              'sample_render_seconds': {'legacy': round(elapsed[0], 4),
                                                        'reused': round(elapsed[1], 4)}}), flush=True)


if __name__ == '__main__':
    main()
