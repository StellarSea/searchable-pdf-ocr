import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
from ocr_to_searchable_pdf import _find_blocks

parser = argparse.ArgumentParser(description='Inspect one page in an OCR layout cache')
parser.add_argument('cache', type=Path)
parser.add_argument('--page', type=int, default=1, help='1-based page number')
args = parser.parse_args()
d = json.loads(args.cache.read_text(encoding='utf-8'))
if not 1 <= args.page <= len(d):
    parser.error('--page is outside the cache')
bs = _find_blocks(d[args.page - 1])
print(len(bs), "blocks")
for t, b in bs:
    print([round(v) for v in b], repr(t[:70]))
