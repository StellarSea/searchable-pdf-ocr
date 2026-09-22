"""Independent page sample and manually transcribed spatial OCR checks."""
import argparse
import hashlib
import json
import random
from collections import Counter
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr
import validate_ocr_documents as validation

OUT = validation.OUT / 'holdout30'


def prepare():
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / 'holdout30.pdf'
    if target.exists():
        raise SystemExit('Sample already exists; keep the frozen sample.')
    prior = json.loads((validation.OUT / 'manifest.json').read_text(encoding='utf-8'))
    rng = random.Random(20260911)
    manifest = []
    with fitz.open() as sample:
        for pattern in ('SQL*.pdf', '*물리학*.pdf', '디지털*.pdf'):
            path = next((validation.ROOT / 'input').glob(pattern))
            excluded = {r['source_page'] - 1 for r in prior if r['source'] == path.name}
            with fitz.open(path) as doc:
                for stratum in range(10):
                    candidates = [i for i in range(stratum * len(doc)//10,
                                                   (stratum+1)*len(doc)//10)
                                  if i not in excluded]
                    index = rng.choice(candidates)
                    sample.insert_pdf(doc, from_page=index, to_page=index)
                    manifest.append({'sample_page': len(sample), 'source': path.name,
                                     'source_page': index+1})
        sample.save(target)
        for start in range(0, len(sample), 6):
            sheet = Image.new('RGB', (1500, 1500), 'white')
            draw = ImageDraw.Draw(sheet)
            for j in range(min(6, len(sample)-start)):
                p = sample[start+j]
                pix = p.get_pixmap(matrix=fitz.Matrix(1, 1))
                im = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                im.thumbnail((490, 710))
                x, y = j%3*500, j//3*750
                sheet.paste(im, (x, y+30))
                draw.text((x+5, y+5), f"Sample {start+j+1} / source {manifest[start+j]['source_page']}", fill='black')
            sheet.save(OUT / f'source_sheet_{start+1}.png')
    script = Path(ocr.__file__)
    ocr.atomic_json(OUT / 'manifest.json', {'seed': 20260911, 'pages': manifest,
                    'excluded_prior_pages': prior,
                    'baseline_code_sha256': hashlib.sha256(script.read_bytes()).hexdigest()})
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def spatial(line_ocr=False):
    gold = json.loads((OUT / 'ground_truth.json').read_text(encoding='utf-8'))
    cache = json.loads((OUT / 'holdout30_pruned.json').read_text(encoding='utf-8'))
    results = []
    suffix = '_line' if line_ocr else ''
    with fitz.open(OUT / f'holdout30{suffix}_searchable.pdf') as doc:
        for item in gold['items']:
            p = doc[item['page']-1]
            target = fitz.Rect([v/gold['render_scale'] for v in item['pixel_bbox']])
            # Use actual PDF character coordinates, not intended insertion boxes.
            chars = []
            for span in p.get_texttrace():
                for code, gid, origin, bbox in span['chars']:
                    r = fitz.Rect(bbox) * p.rotation_matrix
                    chars.append((chr(code), r))
            normalize = lambda s: ''.join(s.split())
            compact = [(c, r) for c, r in chars if not c.isspace()]
            text = ''.join(c for c, r in compact)
            expected = normalize(item['text'])
            raw_text = normalize(''.join(ocr.strip_html(t) for t, b in ocr._find_blocks(cache[item['page']-1])))
            hits = []
            start = text.find(expected)
            while start >= 0:
                hit = compact[start:start+len(expected)]
                inside = sum(target.contains(fitz.Point((r.x0+r.x1)/2, (r.y0+r.y1)/2)) for c, r in hit)
                bounds = fitz.Rect(hit[0][1])
                for c, r in hit[1:]:
                    bounds |= r
                hits.append({'inside_fraction': inside/len(hit), 'bbox': list(bounds)})
                start = text.find(expected, start+1)
            best = max(hits, key=lambda h: h['inside_fraction']) if hits else None
            inside_text = ''.join(c for c, r in chars if target.contains(fitz.Point((r.x0+r.x1)/2, (r.y0+r.y1)/2)))
            passed = bool(best and best['inside_fraction'] >= 0.9 and normalize(inside_text) == expected)
            results.append({**item, 'bbox': list(target), 'passed': passed, 'best_match': best,
                            'expected_present_in_ocr': expected in raw_text,
                            'actual_inside': inside_text,
                            'status': 'pass' if passed else ('misplaced_or_contaminated' if best else 'text_mismatch_or_missing')})
    summary = {'checked': len(results), 'passed': sum(r['passed'] for r in results),
               'checked_pages': sorted({r['page'] for r in results}),
               'ground_truth_sha256': hashlib.sha256((OUT/'ground_truth.json').read_bytes()).hexdigest(),
               'evaluated_code_sha256': hashlib.sha256(Path(ocr.__file__).read_bytes()).hexdigest(),
               'criteria': 'Exact non-whitespace text in manually marked line/cell; >=90% of expected character centers inside. No OCR-derived ground truth.'}
    ocr.atomic_json(OUT / f'spatial{suffix}_audit.json', {'summary': summary, 'items': results})
    with fitz.open(OUT / 'holdout30.pdf') as source, fitz.open() as evidence:
        for pi in sorted({r['page'] for r in results}):
            evidence.insert_pdf(source, from_page=pi-1, to_page=pi-1)
            p = evidence[-1]
            for r in results:
                if r['page'] != pi:
                    continue
                p.draw_rect(fitz.Rect(r['bbox'])*p.derotation_matrix, color=(0,0.6,0), width=0.6)
                if r['best_match']:
                    p.draw_rect(fitz.Rect(r['best_match']['bbox'])*p.derotation_matrix, color=(0.9,0,0), width=0.5)
            p.get_pixmap(matrix=fitz.Matrix(1.5,1.5)).save(OUT/f'spatial{suffix}_page_{pi}.png')
        evidence.save(OUT/f'spatial{suffix}_evidence.pdf', garbage=3, deflate=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def pixels(line_ocr=False):
    def fingerprint(p):
        pix = p.get_pixmap(matrix=fitz.Matrix(0.5,0.5), colorspace=fitz.csGRAY)
        return (pix.width, pix.height, hashlib.sha256(pix.samples).hexdigest())
    previous = set()
    checked_files = []
    for path in [validation.ROOT/'input'/f'{name}.pdf' for name in ('test5','test30','probe2')] + [validation.OUT/'mixed12.pdf']:
        if path.exists():
            with fitz.open(path) as doc:
                previous.update(fingerprint(p) for p in doc)
            checked_files.append(str(path))
    suffix = '_line' if line_ocr else ''
    with fitz.open(OUT/'holdout30.pdf') as source, fitz.open(OUT/f'holdout30{suffix}_searchable.pdf') as result:
        page_count = len(source)
        duplicates = [i+1 for i,p in enumerate(source) if fingerprint(p) in previous]
        changed = []
        for i,p in enumerate(source):
            before = p.get_pixmap(matrix=fitz.Matrix(1,1))
            after = result[i].get_pixmap(matrix=fitz.Matrix(1,1))
            if (before.width,before.height,before.samples)!=(after.width,after.height,after.samples):
                changed.append(i+1)
    report = {'pages':page_count,
              'visually_changed_pages_at_72dpi':changed,
              'exact_render_duplicate_pages_at_36dpi':duplicates,
              'prior_sample_files':checked_files}
    ocr.atomic_json(OUT/f'pixel{suffix}_audit.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


def line_audit():
    pages = json.loads((OUT/'holdout30_pruned.json').read_text(encoding='utf-8'))
    decisions = json.loads((OUT/'holdout30_line_ocr.json').read_text(encoding='utf-8'))['decisions']
    count = lambda text: Counter(c for c in text if not c.isspace())
    differences = []
    with fitz.open(OUT/'holdout30_line_searchable.pdf') as result:
        for pi,pr in enumerate(pages):
            expected = count(''.join(ocr.strip_html(t) for t,b in ocr._find_blocks(pr)))
            for d in decisions:
                if d['page'] == pi+1:
                    expected.subtract(count(ocr.strip_html(d['original'])))
                    expected.update(count(''.join(d['selected'])))
            actual = count(result[pi].get_text())
            if expected != actual:
                differences.append({'page':pi+1,'missing':dict(expected-actual),'extra':dict(actual-expected)})
        invisible = all(s['type']==3 for p in result for s in p.get_texttrace())
    report = {'page_differences':differences,'all_text_invisible':invisible,
              'blocks':len(decisions),'accepted':sum(d['accepted'] for d in decisions),
              'manually_reviewed':sum(d['accepted'] and d['reviewed']!=d['original'] for d in decisions)}
    ocr.atomic_json(OUT/'line_output_audit.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['prepare', 'audit', 'spatial', 'pixels', 'line-audit'])
    ap.add_argument('--line-ocr', action='store_true')
    args = ap.parse_args()
    if args.mode == 'prepare':
        prepare()
    elif args.mode == 'audit':
        validation.OUT = OUT
        validation.audit('holdout30', OUT/'holdout30.pdf', OUT/'holdout30_pruned.json')
    elif args.mode == 'spatial':
        spatial(args.line_ocr)
    elif args.mode == 'line-audit':
        line_audit()
    else:
        pixels(args.line_ocr)
