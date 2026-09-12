import json
import sys

sys.path.insert(0, r"C:\ocr\compose")
from ocr_to_searchable_pdf import _find_blocks

PAGE = 74  # 0부터 시작. 해당 페이지 번호-1
d = json.load(
    open(
        r"C:\ocr\output\ETS 토익 정기시험 기출문제집 1000 Vol. 5 RC_문제_600dpi_pruned.json",
        encoding="utf-8",
    )
)
bs = _find_blocks(d[PAGE])
print(len(bs), "blocks")
for t, b in bs:
    print([round(v) for v in b], repr(t[:70]))
