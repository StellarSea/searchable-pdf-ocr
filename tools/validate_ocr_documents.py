"""Repeatable OCR validation on sampled local documents; no accuracy proxy score."""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output' / 'review_validation'


def prepare():
    OUT.mkdir(exist_ok=True)
    manifest = []
    with fitz.open() as sample:
        for pattern in ('SQL*.pdf', '*물리학*.pdf', '디지털*.pdf'):
            path = next((ROOT / 'input').glob(pattern))
            with fitz.open(path) as doc:
                for index in (9, len(doc)//3, 2*len(doc)//3, len(doc)-10):
                    sample.insert_pdf(doc, from_page=index, to_page=index)
                    manifest.append({'sample_page': len(sample), 'source': path.name,
                                     'source_page': index+1})
        sample.save(OUT / 'mixed12.pdf')
        for start in range(0, len(sample), 4):
            sheet = Image.new('RGB', (1200, 900), 'white')
            draw = ImageDraw.Draw(sheet)
            for j in range(4):
                pix = sample[start+j].get_pixmap(matrix=fitz.Matrix(0.6,0.6))
                im = Image.frombytes('RGB', (pix.width,pix.height), pix.samples)
                im.thumbnail((590,420))
                x,y=(j%2)*600,(j//2)*450
                sheet.paste(im,(x,y+25))
                draw.text((x+8,y+4),f'Sample {start+j+1}; source page {manifest[start+j]["source_page"]}', fill='black')
            sheet.save(OUT / f'sources_{start+1}.png')
    ocr.atomic_json(OUT / 'manifest.json', manifest)


def audit(name, source, cache):
    pages = json.loads(cache.read_text(encoding='utf-8'))
    pdf = OUT / f'{name}_debug.pdf'
    ocr.overlay(source, pages, pdf, debug=True)
    findings = []
    with fitz.open(source) as doc:
        _,_,font = ocr.pick_font(doc)
        for pi, pr in enumerate(pages):
            w,h = ocr._find_size(pr)
            p=doc[pi]
            for bi,(raw,b) in enumerate(ocr._find_blocks(pr)):
                r=fitz.Rect(b[0]*p.rect.width/w,b[1]*p.rect.height/h,
                            b[2]*p.rect.width/w,b[3]*p.rect.height/h) & p.rect
                rects=ocr.detect_lines(p,r)
                table=ocr.layout_table(p,r,raw,rects,font) if rects else None
                if table:
                    rects,chunks=table
                else:
                    chunks=ocr.split_text(raw,rects,font) if rects else []
                expected=ocr.strip_html(raw)
                normalized=lambda s: ''.join(s.split())
                conservation = not rects or normalized(''.join(chunks))==normalized(expected)
                findings.append({'page':pi+1,'block':bi,'units':len(ocr.text_units(raw)),
                                 'rects':len(rects),'chars':len(expected),
                                 'conserved':conservation,'chunks':chunks})
    with fitz.open(pdf) as result, fitz.open(source) as original:
        text=''.join(p.get_text() for p in result)
        invisible=all(span['type']==3 for page in result for span in page.get_texttrace())
        extraction_missing=[]
        for pi, pr in enumerate(pages):
            count=lambda s: Counter(c for c in s if not c.isspace())
            expected=''.join(ocr.strip_html(t) for t,_ in ocr._find_blocks(pr))
            missing=count(expected)-count(result[pi].get_text())
            if missing:
                extraction_missing.append({'page':pi+1,'missing':dict(missing)})
        summary={'name':name,'pages':len(result),'blocks':len(findings),
                 'text_conservation_failures':sum(not b['conserved'] for b in findings),
                 'empty_chunks':sum(sum(not c.strip() for c in b['chunks']) for b in findings),
                 'nul_characters':text.count('\x00'), 'all_text_invisible':invisible,
                 'extraction_missing':extraction_missing}
        for pi in (0,len(result)//2,len(result)-1):
            result[pi].get_pixmap(matrix=fitz.Matrix(1,1)).save(OUT / f'{name}_page{pi+1}.png')
    ocr.atomic_json(OUT / f'{name}_audit.json', {'summary':summary,'blocks':findings})
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return summary


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('mode',choices=['prepare','cached','mixed'])
    args=ap.parse_args()
    if args.mode=='prepare':
        prepare()
    elif args.mode=='cached':
        for name in ('test5','test30'):
            audit(name,ROOT/'input'/f'{name}.pdf',ROOT/'output'/f'{name}_pruned.json')
    else:
        audit('mixed12',OUT/'mixed12.pdf',OUT/'mixed12_pruned.json')
