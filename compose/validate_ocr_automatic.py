"""Run the unattended entry point on real scan pages using verified prior OCR."""
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz
import ocr_to_searchable_pdf as ocr


def main():
    root=Path(__file__).resolve().parents[1]/'output/review_validation'
    previous=root/'holdout30'
    work=root/'automatic_real'
    work.mkdir(exist_ok=True)
    src=work/'auto3.pdf'
    out=work/'ocr_output'
    out.mkdir(exist_ok=True)
    indices=[12,16,25]
    if not src.exists():
        with fitz.open(previous/'holdout30.pdf') as source, fitz.open() as target:
            for i in indices:
                target.insert_pdf(source,from_page=i,to_page=i)
            target.save(src)
        pages=json.loads((previous/'holdout30_pruned.json').read_text(encoding='utf-8'))
        chosen=[pages[i] for i in indices]
        ocr.atomic_json(out/'auto3_pruned.json',chosen)
        ocr.atomic_json(out/'auto3_cache_meta.json',ocr.source_identity(src))
        (out/'auto3.md').write_text(ocr.MD_SEP.join('\n'.join(t for t,b in ocr._find_blocks(p)) for p in chosen),encoding='utf-8')
        shutil.copyfile(previous/'holdout30_line_ocr.json',out/'auto3_line_ocr.json')
    with patch('sys.argv',['ocr',str(src)]), patch.object(ocr,'_call_api',side_effect=AssertionError('Unexpected uncached API request')) as api:
        ocr.main()
        assert api.call_count==0
    with fitz.open(out/'auto3_auto_searchable.pdf') as doc:
        assert len(doc)==3
    report=json.loads((out/'auto3_auto_report.json').read_text(encoding='utf-8'))
    assert not report['validation_failed']
    print('Real scan default workflow verified, no manual review or API calls:',report['status'])


if __name__=='__main__':
    main()
