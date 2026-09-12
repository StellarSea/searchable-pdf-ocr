#!/usr/bin/env python3
"""
PaddleOCR-VL API -> 검색 가능한 PDF (invisible text layer, 줄 단위 정렬)

API는 문단 단위 좌표만 준다. 그래서 문단 영역을 원본에서 렌더링한 뒤
가로 방향 픽셀 투영으로 실제 글자 줄을 찾아내고, 문단 텍스트를 각 줄에
너비 비례로 나눠 넣는다. 추가 OCR 모델 없이 줄 정렬을 맞추는 방식이다.

사용법:
    python ocr_to_searchable_pdf.py input.pdf --out C:\\ocr\\output --toc
    python ocr_to_searchable_pdf.py input.pdf --paragraph    # 예전 문단 방식
    python ocr_to_searchable_pdf.py input.pdf --debug-lines  # 줄 상자를 빨간 테두리로 표시
    python ocr_to_searchable_pdf.py input.pdf --no-cache     # OCR 재실행
    python ocr_to_searchable_pdf.py input.pdf --dump-structure

필요 패키지:
    pip install requests pymupdf pypdf numpy

전제:
    docker compose up 으로 PaddleOCR-VL API가 localhost:8080 에 떠 있어야 함
"""

import argparse
import hashlib
import base64
import html as _html
import io
import json
import os
import re
import sys
import time
import sqlite3
from html.parser import HTMLParser
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import requests

HTML_TAG_RE = re.compile(r"</?(?:table|tr|td|th|p|div|br|span|b|strong|i|em|img)\b", re.I)


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"td", "th", "tr", "p", "div", "br"}:
            self.parts.append("\n")
        elif not HTML_TAG_RE.match(self.get_starttag_text()):
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in {"td", "th", "tr", "p", "div"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def text_units(text):
    """Keep OCR line and HTML cell boundaries; literal angle brackets are text."""
    if HTML_TAG_RE.search(text):
        parser = _TextParser()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    else:
        text = _html.unescape(text)
    text = searchable_math(text)
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def searchable_math(text):
    """Convert only flat, recognized math; leave currency and complex TeX intact."""
    symbols = {'times': '\u00d7', 'cdot': '\u00b7', 'pm': '\u00b1',
               'leq': '\u2264', 'geq': '\u2265', 'neq': '\u2260'}

    def convert(match):
        expr = match.group(1)
        commands = re.findall(r'\\([A-Za-z]+)', expr)
        if not commands or any(c not in symbols for c in commands):
            return match.group(0)
        plain = re.sub(r'\\([A-Za-z]+)', lambda m: symbols[m.group(1)], expr)
        if not re.fullmatch(r'[A-Za-z0-9\s.+*/=()\-\u00d7\u00b7\u00b1\u2264\u2265\u2260]+', plain):
            return match.group(0)
        return plain.strip()

    return re.sub(r'(?<![\\$])\$(?!\$)([^$\n]+)\$(?!\$)', convert, text)


def strip_html(text):
    """표 HTML을 평문으로 편다. 셀/행 경계는 공백으로."""
    return " ".join(text_units(text))


try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        sys.exit("PyMuPDF가 없습니다:  pip install pymupdf")

try:
    import numpy as np
except ImportError:
    sys.exit("numpy가 없습니다:  pip install numpy")

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    sys.exit("pypdf가 없습니다:  pip install pypdf")


BASE = "http://127.0.0.1:8080"
API = f"{BASE}/layout-parsing"
LINE_BASE = "http://127.0.0.1:8081"
LINE_API = f"{LINE_BASE}/recognize"
LINE_ENGINE = "PP-OCRv5_mobile_rec-gpu-320-w4-v2"
# The default recognizer's dictionary holds no Hangul, so Korean lines go to a
# separate model. Each engine string keys its own crop cache; keeping the
# default string unchanged preserves every Latin crop already recognized.
LINE_ENGINE_KOREAN = "korean_PP-OCRv5_mobile_rec-gpu-320-w4-v2"
REPLACEMENT_CHAR = '�'
HANGUL_RE = re.compile(r'[가-힣]')
# The Korean model reads Hangul but no CJK ideographs or kana; the default model
# is the reverse. Latin, digits, Greek and maths symbols are in both, so only
# these two scripts decide which model sees a block.
CJK_RE = re.compile(r'[一-鿿㐀-䶿぀-ヿ]')


def line_language(text):
    """Pick the recognizer whose dictionary covers more of this block."""
    plain = strip_html(text)
    return 'korean' if len(HANGUL_RE.findall(plain)) > len(CJK_RE.findall(plain)) else None
MD_SEP = "\n\n---PAGE---\n\n"

FONT_CANDIDATES = [
    ("malgun", r"C:\Windows\Fonts\malgun.ttf"),  # 폭이 좁아 원본에 가깝다
    ("korea", None),  # PyMuPDF 내장 CJK
    ("japan", None),
    ("gulim", r"C:\Windows\Fonts\gulim.ttc"),
]

TOC_KEEP = re.compile(
    r"(Part\s*\d|Unit\s*\d|Chapter\s*\d|Test\s*\d|DAY\s*\d|정답|해설|목차|부록)",
    re.IGNORECASE,
)

# 줄 검출 파라미터
ZOOM = 3  # 문단 영역 렌더링 배율
DARK = 170  # 이 값보다 어두우면 글자로 본다 (0-255)
MIN_INK = 0.004  # 행이 글자로 인정되려면 필요한 어두운 픽셀 비율
MIN_LINE_PT = 1.5  # 이보다 얇은 줄은 노이즈로 버린다 (pt)
GAP_MERGE_PT = 0.5  # 이보다 좁은 간격은 같은 줄로 합친다 (pt)
RULE_FRAC = 0.9  # 이 비율 이상 채워진 행/열은 글자가 아니라 괘선으로 본다
RULE_WEAK_FRAC = 0.5  # 괘선 심에 이어진 옅은 가장자리는 이 비율까지 함께 지운다
GRAPHIC_H_RATIO = 1.6  # 중앙값보다 이만큼 두껍고
GRAPHIC_INK_RATIO = 0.4  # 잉크가 이만큼 성기면 글자 줄이 아니라 그래픽
CELL_GAP_RATIO = 2.5  # 줄 높이 중앙값의 이 배를 넘는 가로 공백은 셀 경계
CELL_MIN_W_RATIO = 0.3  # 이보다 좁은 조각은 글자가 아니라 얼룩으로 버린다
RULE_MIN_LEN_PT = 20  # 이보다 길게 이어지는 획은 글자가 아니라 괘선 (pt)
DEBRIS_H_RATIO = 0.35  # 줄 높이 중앙값의 이보다 얇은 밴드는 괘선 잔해
RULE_ROW_FRAC = 0.7  # 잉크의 이 비율 이상이 괘선이던 행/열은 통째로 지운다
SPECK_W = 3  # 이보다 좁은 잉크 덩어리는 얼룩/셀 구분선으로 보고 셀 경계 계산에서 뺀다
DITHER_RATE = 0.15  # 행마다 켜졌다 꺼지는 비율이 이 이상인 열은 디더 채움
REPAIR_RATIO = 0.4  # 글자 수가 기대치의 이 비율에 못 미치면 본문이 유실된 블록
REPAIR_MIN_CHARS = 200  # 단, 기대치가 이보다 큰 블록만 (작은 블록은 오탐)
CHAR_W_RATIO = 0.46  # 글자 폭 ~ 줄 높이의 이 배 (기대 글자 수 추정용)


# ---------------------------------------------------------------- API 호출


API_OPTIONS = {"visualize": False, "useChartRecognition": False,
               "useSealRecognition": False, "mergeLayoutBlocks": False, "layoutNms": True}


def _call_api(data: bytes, file_type: int, timeout=600, options=None, attempts=3) -> dict:
    payload = {
        "file": base64.b64encode(data).decode(),
        "fileType": file_type,
        **API_OPTIONS,
        **(options or {}),
    }
    for attempt in range(attempts):
        try:
            r = requests.post(API, json=payload, timeout=(10, timeout or 600))
            r.raise_for_status()
            break
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as ex:
            status = getattr(getattr(ex, 'response', None), 'status_code', None)
            if attempt == attempts - 1 or (status is not None and status not in (429,500,502,503,504)):
                raise
            print(f'[retry] API attempt {attempt+2}/{attempts}', flush=True)
            time.sleep(2**(attempt+1))
    body = r.json()
    if body.get("errorCode", 0) != 0:
        raise RuntimeError(f"API error {body['errorCode']}: {body.get('errorMsg')}")
    return body["result"]


def ocr_pdf(pdf_bytes: bytes, timeout=None) -> dict:
    return _call_api(pdf_bytes, 0, timeout)


def ocr_image(png_bytes: bytes, timeout=None) -> str:
    """이미지 한 장을 읽어 블록 내용을 이어붙인다."""
    res = _call_api(png_bytes, 1, timeout)
    out = []
    for r in res.get("layoutParsingResults", []):
        for b in r.get("prunedResult", {}).get("parsing_res_list", []):
            c = b.get("block_content")
            if isinstance(c, str) and c.strip():
                out.append(c)
    return "\n".join(out)


def recognize_lines(png_images, timeout=120, lang=None):
    """Recognize many line crops in one request to the lightweight CPU service."""
    payload = {'images': [base64.b64encode(data).decode() for data in png_images]}
    if lang:
        payload['lang'] = lang
    r = requests.post(LINE_API, json=payload, timeout=(10, timeout))
    r.raise_for_status()
    body = r.json()
    values = body.get('texts')
    if not isinstance(values, list) or len(values) != len(png_images):
        raise RuntimeError('Line OCR returned an unexpected result count')
    if not all(isinstance(value, str) for value in values):
        raise RuntimeError('Line OCR returned invalid text')
    return values


OCR_RENDER_ZOOM = 2.0


def needs_flattening(src: Path) -> bool:
    """True when a page carries a rotation or a crop box the layout model mishandles.

    On a book whose pages were saved rotated and cropped, the layout model
    returned less than half the text it finds on the very same pixels rendered
    upright: one scan lost a whole table and a code listing per page. Books with
    neither are unaffected, so they keep their existing caches and results.
    """
    with fitz.open(src) as doc:
        return any(page.rotation or tuple(page.cropbox) != tuple(page.mediabox)
                   for page in doc)


def _flattened(doc, start, stop):
    """Render pages to exactly the pixels page.rect describes, with no rotation."""
    out = fitz.open()
    try:
        for index in range(start, stop):
            pix = doc[index].get_pixmap(matrix=fitz.Matrix(OCR_RENDER_ZOOM, OCR_RENDER_ZOOM))
            page = out.new_page(width=pix.width/OCR_RENDER_ZOOM,
                                height=pix.height/OCR_RENDER_ZOOM)
            page.insert_image(page.rect, pixmap=pix)
        return out.tobytes(garbage=3, deflate=True)
    finally:
        out.close()


def split_pdf(path: Path, size: int):
    if needs_flattening(path):
        with fitz.open(path) as doc:
            total = len(doc)
            for start in range(0, total, size):
                yield start, _flattened(doc, start, min(start + size, total)), total
        return
    reader = PdfReader(str(path))
    total = len(reader.pages)
    for start in range(0, total, size):
        w = PdfWriter()
        for p in reader.pages[start : start + size]:
            w.add_page(p)
        buf = io.BytesIO()
        w.write(buf)
        yield start, buf.getvalue(), total


# ------------------------------------------------- prunedResult 구조 탐색

BLOCK_LIST_KEYS = ["parsing_res_list"]
TEXT_KEYS = ["block_content", "text", "rec_text", "content", "label_content"]
BBOX_KEYS = ["block_bbox", "bbox", "box", "coordinate", "layout_bbox", "rec_box"]
SIZE_KEYS = ["input_img_size", "img_size", "image_size", "page_size", "shape"]


def _norm_bbox(b):
    flat = []
    for v in b:
        if isinstance(v, (list, tuple)):
            flat.extend(v)
        else:
            flat.append(v)
    flat = [float(v) for v in flat if isinstance(v, (int, float))]
    if len(flat) < 4:
        return None
    if len(flat) == 4:
        x0, y0, x1, y1 = flat
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
    else:
        xs, ys = flat[0::2], flat[1::2]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return [x0, y0, x1, y1]


def _find_blocks(pruned: dict):
    out, seen = [], set()

    def take(node):
        if not isinstance(node, dict):
            return False
        text = next(
            (
                node[k]
                for k in TEXT_KEYS
                if isinstance(node.get(k), str) and node[k].strip()
            ),
            None,
        )
        raw = next(
            (
                node[k]
                for k in BBOX_KEYS
                if isinstance(node.get(k), (list, tuple)) and len(node[k]) >= 4
            ),
            None,
        )
        if not (text and raw):
            return False
        bbox = _norm_bbox(raw)
        if not bbox:
            return False
        key = (" ".join(text.split()), tuple(bbox))
        if key in seen:
            return True
        seen.add(key)
        out.append((text, bbox))
        return True

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            if take(node):  # 여기서 잡았으면 하위로 안 내려간다
                return
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    for key in BLOCK_LIST_KEYS:
        if key in pruned:
            walk(pruned[key])
            if out:
                return out
    walk(pruned)
    return out


def _find_size(pruned: dict):
    w, h = pruned.get("width"), pruned.get("height")
    if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
        return (float(w), float(h))
    for k in SIZE_KEYS:
        v = pruned.get(k)
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            a, b = float(v[0]), float(v[1])
            return (b, a) if k == "shape" else (a, b)
    return None


# ------------------------------------------------------------- 줄 검출


def _long_runs(mask, length):
    """가로로 length 이상 이어지는 True 런만 남긴다 (1xL 열림 연산).

    표/박스 테두리를 글자와 가르는 유일하게 믿을 만한 신호가 획 길이다.
    잉크 비율로는 못 가른다. 밴드는 줄의 타이트한 잉크 범위라서 d, l, P 같은
    글자 세로획이 밴드 높이를 그대로 채우기 때문이다.
    """
    h, n = mask.shape
    if length < 2 or n < length:
        return np.zeros_like(mask)
    # 침식: 길이 length 창이 전부 True인 시작 위치
    c = np.pad(np.cumsum(mask, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
    ero = (c[:, length:] - c[:, :-length]) == length
    # 팽창: 그 창들이 덮는 칸을 되살린다
    m = ero.shape[1]
    ce = np.pad(np.cumsum(ero, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
    j = np.arange(n)
    hi = np.minimum(j + 1, m)
    lo = np.maximum(j - length + 1, 0)
    return (ce[:, hi] - ce[:, lo]) > 0


def detect_lines(page, rect):
    """문단 영역을 렌더링해 가로 투영으로 실제 글자 줄 상자를 찾는다."""
    if rect.width < 4 or rect.height < 4:
        return []
    try:
        pix = page.get_pixmap(
            matrix=fitz.Matrix(ZOOM, ZOOM),
            clip=rect,
            colorspace=fitz.csGRAY,
            alpha=False,
        )
    except Exception:
        return []
    if pix.width < 2 or pix.height < 2:
        return []

    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    dark = arr < DARK

    # 스크롤바 트랙이나 음영은 디더(체커보드)로 찍혀 있어 길이 기준 괘선 제거에
    # 걸리지 않는다. 그대로 두면 거의 모든 밴드에 좁은 조각이 하나씩 생겨
    # 줄마다 단어를 하나씩 빼앗는다. 글자 획은 세로로 이어지지만 디더는 한 행
    # 걸러 끊긴다 (본문 열 최대 0.089 대 스크롤바 0.31).
    if pix.height >= 8:
        churn = (dark[1:] != dark[:-1]).sum(axis=0) / pix.height
        dither = churn >= DITHER_RATE
        if 0 < dither.sum() <= pix.width * 0.5:
            dark[:, dither] = False

    # 표/박스 테두리를 형태학적 열림으로 먼저 걷어낸다. 박스는 위아래 가로선이
    # 상자 폭을 통째로 채워서 셀 안 모든 열에 잉크를 남긴다. 그러면 단어 사이
    # 공백이 물리적으로 사라져 어떤 간격 기준으로도 글자 범위를 되찾을 수 없다.
    rule_len = int(RULE_MIN_LEN_PT * ZOOM)
    hrule = _long_runs(dark, rule_len)
    vrule = _long_runs(dark.T, rule_len).T
    if hrule.any() or vrule.any():
        ink_row = dark.sum(axis=1)
        ink_col = dark.sum(axis=0)
        dark &= ~(hrule | vrule)
        # 괘선을 지우면 톱니 같은 부스러기가 남는다(스캔이 미세하게 기울어
        # 획이 여러 행에 걸치기 때문). 그대로 두면 이웃 글자 줄에 들러붙어
        # 그 줄의 잉크 범위를 상자 끝까지 늘리고 셀 분할까지 어긋난다.
        # 원래 잉크의 대부분이 괘선이던 행/열은 남은 것도 괘선으로 본다.
        row_gone = hrule.sum(axis=1) >= RULE_ROW_FRAC * np.maximum(ink_row, 1)
        col_gone = vrule.sum(axis=0) >= RULE_ROW_FRAC * np.maximum(ink_col, 1)
        dark[row_gone & (ink_row > 0)] = False
        dark[:, col_gone & (ink_col > 0)] = False

    # 세로 괘선(표/스크린샷 테두리)은 모든 행에 잉크를 남겨 행 투영을 포화시킨다.
    # 폭 1286px 블록의 테두리 7px만으로 0.54% > MIN_INK 라 줄 간격이 사라진다.
    # 여러 줄이 들어갈 높이일 때만 적용한다 (한 줄짜리는 글자 세로획이 걸린다).
    if pix.height >= 3 * MIN_LINE_PT * ZOOM:
        colf = dark.mean(axis=0)
        strong = colf >= RULE_FRAC
        # 괘선 가장자리는 안티에일리어싱으로 옅어져 임계값을 못 넘긴다. 남은
        # 1~2열이 모든 밴드의 잉크 범위를 페이지 끝까지 늘려 짧은 줄에까지
        # 폭을 한가득 주므로, 진한 심에 이어진 옅은 열까지 한 덩어리로 지운다.
        weak = colf >= RULE_WEAK_FRAC
        vrule = np.zeros_like(weak)
        starts = np.flatnonzero(weak & ~np.r_[False, weak[:-1]])
        ends = np.flatnonzero(weak & ~np.r_[weak[1:], False]) + 1
        for a, b in zip(starts, ends):
            if strong[a:b].any():
                vrule[a:b] = True
        if 0 < vrule.sum() <= pix.width * 0.3:
            dark[:, vrule] = False

    row_ink = dark.sum(axis=1)
    active = row_ink > max(1.0, pix.width * MIN_INK)
    # 가로 괘선은 글자 줄이 아니다. 남겨두면 위아래 줄이 한 밴드로 이어진다.
    active &= dark.mean(axis=1) < RULE_FRAC
    if not active.any():
        return []

    # 연속 구간(밴드) 추출
    bands, start = [], None
    for i, a in enumerate(active):
        if a and start is None:
            start = i
        elif not a and start is not None:
            bands.append([start, i])
            start = None
    if start is not None:
        bands.append([start, len(active)])

    # 좁은 간격은 병합 (자모 분리, 위첨자 등)
    gap_px = GAP_MERGE_PT * ZOOM
    merged = []
    for b in bands:
        if merged and b[0] - merged[-1][1] <= gap_px:
            merged[-1][1] = b[1]
        else:
            merged.append(b)

    min_px = MIN_LINE_PT * ZOOM
    cand = [(a, b) for a, b in merged if b - a >= min_px]

    # 장식용 그래픽 밴드(아이콘 띠, 로고)를 글자 줄로 세면 첫 청크가 거기
    # 들어가 이후 줄이 통째로 한 칸씩 밀린다. 텍스트 줄보다 유난히 두꺼우면서
    # 동시에 잉크가 성기다는 점으로 가른다. 둘 중 하나만으로는 두 줄이 붙은
    # 밴드(두껍지만 진하다)나 짧은 마지막 줄(성기지만 얇다)까지 버리게 된다.
    if len(cand) >= 4:
        hs = [b - a for a, b in cand]
        inks = [dark[a:b].mean() for a, b in cand]
        mh, mi = float(np.median(hs)), float(np.median(inks))
        # 괘선을 걷어내면 그 자리에 끊긴 조각들이 얇은 밴드로 남는다. 잘린
        # 자리가 같은 행이라 세로 팽창으로는 못 지우고, 틈 메우기로 지우려면
        # 글자끼리 붙을 위험이 크다. 두께로 거른다: 잔해는 중앙값의 0.17배,
        # 가장 얇은 실제 줄은 0.69배로 사이가 넉넉히 벌어진다.
        cand = [
            ab
            for ab, h, ink in zip(cand, hs, inks)
            if h >= DEBRIS_H_RATIO * mh
            and not (h > GRAPHIC_H_RATIO * mh and ink < GRAPHIC_INK_RATIO * mi)
        ]

    if not cand:
        return []

    # 표 셀/탭처럼 가로로 크게 벌어진 덩어리는 각각 따로 상자를 준다. 한 줄로
    # 묶으면 잉크는 전체 폭인데 글자는 몇 자뿐이라, 폭 비례 배분이 뒷줄 텍스트를
    # 통째로 끌어온다. 기준 길이는 밴드 제 높이가 아니라 줄 높이 중앙값이다
    # (두 줄이 붙은 밴드가 부당하게 큰 허용치를 받으면 안 된다).
    med_h = float(np.median([b - a for a, b in cand]))
    cell_gap = max(1.0, CELL_GAP_RATIO * med_h)
    min_w = CELL_MIN_W_RATIO * med_h

    out = []
    for a, b in cand:
        nz = np.nonzero(dark[a:b].sum(axis=0) > 0)[0]
        if nz.size == 0:
            continue
        # 스캔 얼룩과 행 높이짜리 셀 구분선이 라벨과 값 사이 공백에 끼면 큰
        # 공백이 잘게 쪼개져 셀 경계를 놓친다 ("To:" 행에서 172px 공백이
        # 53px로 보였다). 구분선은 행만큼만 길어서 괘선 길이 기준에 안 걸리고,
        # 둘 다 폭이 몇 px뿐이라는 게 공통점이라 폭으로 함께 거른다.
        runs = np.split(nz, np.flatnonzero(np.diff(nz) - 1 > 0) + 1)
        solid = [q for q in runs if q[-1] + 1 - q[0] >= SPECK_W]
        if solid:
            nz = np.concatenate(solid)
        for piece in np.split(nz, np.flatnonzero(np.diff(nz) - 1 > cell_gap) + 1):
            if piece[-1] + 1 - piece[0] < min_w:
                continue
            out.append(
                fitz.Rect(
                    (pix.x + piece[0]) / ZOOM,
                    (pix.y + a) / ZOOM,
                    (pix.x + piece[-1] + 1) / ZOOM,
                    (pix.y + b) / ZOOM,
                )
            )
    return out


def split_text(text, rects, fontobj):
    """Assign complete OCR units to boxes before wrapping inside each unit."""
    units = text_units(text)
    if not units or not rects:
        return []
    n, m = len(rects), len(units)
    if m == 1 or m > n or n > 250:
        return _split_words(" ".join(units), rects, fontobj)
    return [chunk for unit, boxes in _assign_units(units, rects, fontobj)
            for chunk in _split_words(unit, boxes, fontobj)]


def _assign_units(units, rects, fontobj):
    n, m = len(rects), len(units)
    widths = np.array([max(r.width, 0.01) for r in rects])
    lengths = np.array([max(fontobj.text_length(u, fontsize=1), 0.01) for u in units])
    scale = widths.sum() / lengths.sum()
    prefix = np.r_[0.0, np.cumsum(widths)]
    costs = np.full((m + 1, n + 1), np.inf)
    previous = np.full((m + 1, n + 1), -1, dtype=int)
    costs[0, 0] = 0
    # A unit owns consecutive boxes. A noisy box cannot consume the next cell's words.
    for u in range(1, m + 1):
        for end in range(u, n - (m - u) + 1):
            for start in range(u - 1, end):
                ratio = (prefix[end] - prefix[start]) / (lengths[u - 1] * scale)
                cost = costs[u - 1, start] + float(np.log(ratio)) ** 2
                if cost < costs[u, end]:
                    costs[u, end], previous[u, end] = cost, start
    groups = []
    end = n
    for u in range(m, 0, -1):
        start = int(previous[u, end])
        groups.append((units[u - 1], rects[start:end]))
        end = start
    return list(reversed(groups))


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.span, self.supported = 1, True

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th'):
            self.cell = []
            try:
                self.span = int(attrs.get('colspan', '1'))
                self.supported &= int(attrs.get('rowspan', '1')) == 1 and self.span > 0
            except ValueError:
                self.supported = False
        elif tag == 'br' and self.cell is not None:
            self.cell.append('\n')

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None and self.row is not None:
            self.row.append((''.join(self.cell).strip(), self.span))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def layout_table(page, rect, text, rects, fontobj):
    """Use repeated column positions for regular HTML tables without row spans."""
    if not text.lstrip().lower().startswith('<table') or text.lower().count('<table') != 1:
        return None
    parser = _TableParser()
    parser.feed(text)
    rows = parser.rows
    if not parser.supported or not rows:
        return None
    columns = max(sum(span for _, span in row) for row in rows)
    if columns < 3 or any(sum(span for _, span in row) != columns for row in rows):
        return None
    if sum(len(row) == columns for row in rows) < 3:
        return None
    bands = []
    for r in rects:
        if bands and abs(bands[-1][0].y0 - r.y0) < 0.1:
            bands[-1].append(r)
        else:
            bands.append([r])
    anchors = [band for band in bands if len(band) == columns]
    if not anchors or len(rows) > len(bands) or len(bands) > 250:
        return None
    boundaries = [rect.x0] + [float(np.median([(b[i].x1 + b[i+1].x0)/2
                                              for b in anchors]))
                              for i in range(columns-1)] + [rect.x1]
    band_rects = [fitz.Rect(min(r.x0 for r in band),band[0].y0,
                           max(r.x1 for r in band),band[0].y1) for band in bands]
    units = [' '.join(strip_html(t) for t,_ in row) or ' ' for row in rows]
    groups = _assign_units(units, band_rects, fontobj)
    pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=rect,
                          colorspace=fitz.csGRAY, alpha=False)
    ink = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < DARK
    # Remove grid lines over the whole table, not inside a tight one-line cell:
    # inside a cell, ordinary stems can span its entire height.
    hr = _long_runs(ink, int(RULE_MIN_LEN_PT * ZOOM))
    vr = _long_runs(ink.T, int(RULE_MIN_LEN_PT * ZOOM)).T
    rows_gone = hr.sum(axis=1) >= RULE_ROW_FRAC * np.maximum(ink.sum(axis=1), 1)
    cols_gone = vr.sum(axis=0) >= RULE_ROW_FRAC * np.maximum(ink.sum(axis=0), 1)
    ink &= ~(hr | vr)
    ink[rows_gone] = False
    ink[:, cols_gone] = False
    out_rects, chunks = [], []
    for row, (_, physical) in zip(rows, groups):
        col = 0
        for value, span in row:
            if value.strip():
                cell = fitz.Rect(boundaries[col],physical[0].y0,
                                 boundaries[col+span],physical[-1].y1)
                if len(physical) == 1:
                    x0 = max(0, int(np.floor(cell.x0 * ZOOM - pix.x)))
                    x1 = min(pix.width, int(np.ceil(cell.x1 * ZOOM - pix.x)))
                    y0 = max(0, int(np.floor(cell.y0 * ZOOM - pix.y)))
                    y1 = min(pix.height, int(np.ceil(cell.y1 * ZOOM - pix.y)))
                    sub = ink[y0:y1, x0:x1]
                    ys, xs = np.nonzero(sub)
                    if xs.size:
                        cell = fitz.Rect((pix.x+x0+int(xs.min()))/ZOOM,
                                         (pix.y+y0+int(ys.min()))/ZOOM,
                                         (pix.x+x0+int(xs.max())+1)/ZOOM,
                                         (pix.y+y0+int(ys.max())+1)/ZOOM)
                    out_rects.append(cell)
                    chunks.append(strip_html(value))
                else:
                    detected = detect_lines(page, cell) or [cell]
                    out_rects.extend(detected)
                    chunks.extend(split_text(value, detected, fontobj))
            col += span
    if ''.join(''.join(chunks).split()) != ''.join(strip_html(text).split()):
        return None
    return out_rects, chunks


def _split_words(text, rects, fontobj):
    """문단 텍스트를 각 줄 상자 폭에 맞춰 실제 글자 폭 기준으로 나눈다."""
    if re.search(r'[\uac00-\ud7a3]', text) and len(rects) > 1:
        return _split_korean(text, rects, fontobj)
    words = " ".join(text.split()).split(" ")
    if not words or not rects:
        return []
    if len(rects) == 1:
        return [" ".join(words)]

    joined = " ".join(words)
    total_r = sum(r.width for r in rects)
    try:
        total_w = fontobj.text_length(joined, fontsize=10.0)
    except Exception:
        total_w = len(joined) * 5.0
    fs = 10.0 * total_r / total_w if total_w > 0 else 10.0

    def width(s):
        try:
            return fontobj.text_length(s, fontsize=fs)
        except Exception:
            return len(s) * fs * 0.5

    chunks, i = [], 0
    for idx, r in enumerate(rects):
        if idx == len(rects) - 1:
            chunks.append(" ".join(words[i:]))
            break
        # 최적 맞춤: 단어를 더 넣었을 때 줄 상자 폭에서 오히려 멀어지면 멈춘다.
        # 폭이 단조 증가하므로 |폭-상자폭| 은 V자, 첫 증가 지점이 최적이다.
        # 단순히 "폭을 넘지 않을 때까지" 넣으면, 상자가 실제 잉크 범위라
        # 대체 폰트 오차 몇 %에 마지막 단어가 밀려 이후 줄이 통째로 어긋난다.
        buf, cur = [], 0.0
        while i < len(words):
            w = width(" ".join(buf + [words[i]]))
            if buf and abs(w - r.width) > abs(cur - r.width):
                break
            buf.append(words[i])
            cur = w
            i += 1
        chunks.append(" ".join(buf))
    return chunks


def _split_korean(text, rects, fontobj):
    """Allow Korean syllable breaks, keeping Latin words and numbers intact.

    Match cumulative widths so a locally rounded break does not shift every
    following line. Explicit OCR units and table cells remain separate upstream.
    """
    text = ' '.join(text.split())
    atoms = re.findall(r'[\uac00-\ud7a3]|[^\s\uac00-\ud7a3]+|\s+', text)
    widths = [fontobj.text_length(s, fontsize=1) for s in atoms]
    prefix = np.r_[0.0, np.cumsum(widths)]
    total = sum(r.width for r in rects)
    if not atoms or total <= 0:
        return [''] * len(rects)
    chunks, start, consumed = [], 0, 0.0
    closing = set('.,!?;:)]}')
    for index, rect in enumerate(rects):
        if index == len(rects)-1:
            chunks.append(''.join(atoms[start:]).strip())
            break
        consumed += rect.width
        target = prefix[-1] * consumed / total
        # A boundary before whitespace belongs after it; never start punctuation.
        candidates = [i for i in range(start+1, len(atoms)+1)
                      if i == len(atoms) or
                      (not atoms[i].isspace() and atoms[i][0] not in closing
                       and atoms[i-1][-1] not in '([{')]
        if not candidates:
            chunks.append('')
            continue
        end = min(candidates, key=lambda i: abs(prefix[i] - target))
        chunks.append(''.join(atoms[start:end]).strip())
        start = end
    return chunks


# ------------------------------------------------------------ 유실 블록 수선


def _expected_chars(rects):
    if not rects:
        return 0.0
    mh = float(np.median([r.height for r in rects]))
    return sum(r.width for r in rects) / max(0.1, CHAR_W_RATIO * mh)


def repair_is_safe(old, new):
    old, new = strip_html(old), strip_html(new)
    if len(new) <= max(len(old) * 1.3, 20):
        return False
    tokens = lambda s: Counter(re.findall(r"\w+", s.casefold()))
    before, after = tokens(old), tokens(new)
    coverage = sum((before & after).values()) / max(1, sum(before.values()))
    critical = lambda s: set(re.findall(r"[\w.+-]+@[\w.-]+|\d+(?:[.,-]\d+)*", s.casefold()))
    return (not before or coverage >= 0.95) and critical(old) <= critical(new)


def repair_blocks(pdf_path, pages_pruned, timeout=None, on_update=None, resume=False):
    """표로 오인돼 본문이 통째로 빠진 블록을 잘라 다시 읽는다.

    이메일/양식은 layout 검출이 table 로 잡는데, 표 인식이 격자 밖 문단을
    버려서 머리글만 남는 일이 있다 (274pt 높이 블록에 143자). 페이지 전체를
    useLayoutDetection=False 로 읽으면 본문은 살지만 좌표가 통째로 사라지고,
    같은 설정으로 블록만 잘라 보내면 본문과 주소가 모두 돌아온다.
    """
    doc = fitz.open(str(pdf_path))
    fixed = 0
    for pno, pruned in enumerate(pages_pruned):
        if pno >= len(doc) or not isinstance(pruned, dict):
            continue
        entries = pruned.get("parsing_res_list")
        if not isinstance(entries, list):
            continue
        page = doc[pno]
        size = _find_size(pruned)
        if not size or size[0] <= 0 or size[1] <= 0:
            continue
        sx, sy = page.rect.width / size[0], page.rect.height / size[1]
        for e in entries:
            if not isinstance(e, dict):
                continue
            if resume and 'repair_audit' in e:
                continue
            text, raw = e.get("block_content"), e.get("block_bbox")
            if not isinstance(text, str) or not isinstance(raw, (list, tuple)):
                continue
            have = len(strip_html(text))
            bbox = _norm_bbox(raw)
            if not bbox:
                continue
            rect = fitz.Rect(bbox[0] * sx, bbox[1] * sy, bbox[2] * sx, bbox[3] * sy)
            rect &= page.rect
            if rect.is_empty or rect.width < 4 or rect.height < 4:
                continue
            rects = detect_lines(page, rect)
            if len(rects) < 3:
                continue
            exp = _expected_chars(rects)
            if exp < REPAIR_MIN_CHARS or have >= REPAIR_RATIO * exp:
                continue
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=rect)
                got = ocr_image(pix.tobytes("png"), timeout=timeout)
            except Exception as ex:
                print(f"[repair] p{pno + 1} 실패: {ex}", flush=True)
                e['repair_error'] = str(ex)
                if on_update:
                    on_update()
                    raise
                continue
            e.pop('repair_error', None)
            e["repair_audit"] = {"original": text, "candidate": got,
                                 "accepted": repair_is_safe(text, got)}
            if e["repair_audit"]["accepted"]:
                print(
                    f"[repair] p{pno + 1} {have} -> {len(strip_html(got))}자 "
                    f"(기대 {int(exp)})",
                    flush=True,
                )
                e["block_content"] = got
                fixed += 1
            else:
                print(f"[repair] p{pno + 1} candidate rejected; original retained", flush=True)
            if on_update:
                on_update()
    doc.close()
    print(f"[repair] {fixed} blocks re-read")
    return fixed


# ------------------------------------------------------- 텍스트 레이어 삽입


def pick_font(doc):
    page = doc[0]
    for name, path in FONT_CANDIDATES:
        try:
            if path:
                if not Path(path).exists():
                    continue
                page.insert_font(fontname=name, fontfile=path)
                fo = fitz.Font(fontfile=path)
            else:
                page.insert_font(fontname=name)
                fo = fitz.Font(name)
            return name, path, fo
        except Exception:
            continue
    return None, None, None


def fit_size(fontobj, text, rect):
    """텍스트 폭이 줄 상자 폭과 맞도록 폰트 크기를 정한다."""
    if not text:
        return 1.0
    try:
        w = fontobj.text_length(text, fontsize=10.0)
    except Exception:
        w = len(text) * 6.0
    fs = 10.0 * rect.width / w if w > 0 else rect.height
    return max(1.0, min(fs, rect.height * 1.35, 60.0))


_INSTALLED_FONTS = None
_FONT_FOR_CODEPOINT = {}


def font_for_codepoint(code):
    """Search every installed font for a glyph, once per codepoint.

    Recognition sometimes returns a character that belongs to no script in the
    book at all -- a Thai vowel or a rupee sign misread from a Korean physics
    page. Insertion failure is fatal by design, so without this a single such
    character throws away a whole finished book. The glyph almost always exists
    in some installed font, so the text is kept rather than the run discarded.
    """
    if code in _FONT_FOR_CODEPOINT:
        return _FONT_FOR_CODEPOINT[code]
    global _INSTALLED_FONTS
    if _INSTALLED_FONTS is None:
        directories = [Path(os.environ.get('SystemRoot', r'C:\Windows'))/'Fonts',
                       Path(os.environ.get('LOCALAPPDATA', ''))/'Microsoft/Windows/Fonts']
        _INSTALLED_FONTS = sorted({p for d in directories if d.is_dir()
                                   for p in d.glob('*.tt*')})
    found = None
    for path in _INSTALLED_FONTS:
        try:
            candidate = fitz.Font(fontfile=str(path))
            if candidate.has_glyph(code):
                found = (f'ocrx{code:04x}', candidate, str(path))
                break
        except Exception:
            continue
    if found:
        print(f'[font] U+{code:04X} taken from {Path(found[2]).name}', flush=True)
    _FONT_FOR_CODEPOINT[code] = found
    return found


def insert_invisible_line(page, text, rect, fontname, fontobj, fallback_fonts):
    runs = []
    for char in text:
        chosen = (fontname, fontobj, None)
        if not char.isspace() and not fontobj.has_glyph(ord(char)):
            chosen = next((f for f in fallback_fonts if f[1].has_glyph(ord(char))), None)
            if chosen is None:
                chosen = font_for_codepoint(ord(char))
            if chosen is None:
                raise ValueError(f"No font for U+{ord(char):04X}")
        if runs and runs[-1][0] == chosen:
            runs[-1][1] += char
        else:
            runs.append([chosen, char])
    width = sum(fo.text_length(s, fontsize=1) for (_, fo, _), s in runs)
    fs = min(rect.width / max(width, 0.01), rect.height * 1.35, 60.0)
    x, y = rect.x0, rect.y1 - rect.height * 0.18
    for (name, fo, path), s in runs:
        if name != fontname:
            page.insert_font(fontname=name, fontfile=path)
        point = fitz.Point(x, y) * page.derotation_matrix
        page.insert_text(point, s, fontname=name, fontsize=fs, render_mode=3,
                         rotate=page.rotation)
        x += fo.text_length(s, fontsize=fs)


def line_refinement_is_safe(original, candidates):
    """Require near-identical content and preserve critical tokens, not just length."""
    if not candidates or any(not c.strip() for c in candidates):
        return False
    normalize = lambda s: ''.join(strip_html(s).split())
    before, after = normalize(original), normalize(''.join(candidates))
    if not before or SequenceMatcher(None, before, after, autojunk=False).ratio() < 0.94:
        return False
    critical = lambda s: Counter(re.findall(r'[\w.+-]+@[\w.-]+|\d+(?:[.,-]\d+)*', s))
    return critical(before) == critical(after)


def recognition_disagrees(original, candidates, threshold=0.97):
    """Flag a block only when the line recognizer read something materially different.

    The inserted text is always the original either way, so this drives the
    review report, not the output. An exact comparison flagged almost every page
    because the small line model never matches character for character; a near
    match is not worth a reviewer's time, but any changed digit or address is.
    """
    normalize = lambda value: ''.join(strip_html(value).split())
    before, after = normalize(original), normalize(''.join(candidates))
    if not before or not after:
        return True
    if SequenceMatcher(None, before, after, autojunk=False).ratio() < threshold:
        return True
    critical = lambda s: Counter(re.findall(r'[\w.+-]+@[\w.-]+|\d+(?:[.,-]\d+)*', s))
    return critical(before) != critical(after)


def line_boundaries_are_usable(original, candidates):
    """Accept approximate OCR only as a boundary guide; its characters are never copied."""
    if not candidates or any(not candidate.strip() for candidate in candidates):
        return False
    normalize = lambda value: ''.join(strip_html(value).split()).casefold()
    before, after = normalize(original), normalize(''.join(candidates))
    if not before or not after:
        return False
    length_ratio = len(after) / len(before)
    return 0.75 <= length_ratio <= 1.25 and SequenceMatcher(
        None, before, after, autojunk=False).ratio() >= 0.88


class LineRefiner:
    """Opt-in crop OCR with an independent, image-addressed cache and decision log."""

    def __init__(self, path, pages, review_path=None, automatic=False, service=None):
        self.path, self.pages = path, pages
        self.options = {'useLayoutDetection': False}
        self.cache = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        self.cache.setdefault('responses', {})
        self.cache['decisions'] = []
        self.unavailable = None
        self.reviews = json.loads(review_path.read_text(encoding='utf-8')) if review_path else {}
        self.used_reviews = set()
        self.automatic, self.service = automatic, service
        self.db = None
        if automatic:
            self.db = sqlite3.connect(path.with_suffix('.sqlite3'))
            self.db.execute('CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
            self.db.executemany('INSERT OR IGNORE INTO responses VALUES (?,?)', self.cache['responses'].items())
            self.db.execute('DELETE FROM decisions')
            self.db.commit()
            self.cache['responses'] = {}

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def response(self, key):
        if self.db is not None:
            row = self.db.execute('SELECT value FROM responses WHERE key=?', (key,)).fetchone()
            return row[0] if row else None
        return self.cache['responses'].get(key)

    def refine(self, page, original, rects, chunks):
        candidates = []
        error = None
        keys = []
        # A block is one language in practice. The Korean model also carries the
        # Latin alphabet and digits, so mixed Korean/English blocks are safe on
        # it, while pure-Latin blocks stay on the better-tested default model.
        lang = line_language(original)
        engine = LINE_ENGINE_KOREAN if lang else LINE_ENGINE
        try:
            prepared = []
            for rect in rects:
                crop = fitz.Rect(rect.x0-1, rect.y0-0.7, rect.x1+1, rect.y1+0.7) & page.rect
                pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=crop,
                                      colorspace=fitz.csRGB, alpha=False)
                png = pix.tobytes('png')
                settings = json.dumps({'api': LINE_API, 'engine': engine}, sort_keys=True)
                key = hashlib.sha256(settings.encode()+png).hexdigest()
                keys.append(key)
                value = self.response(key)
                prepared.append((key, png, value))

            # One block can contain many detected lines. Send every missing crop
            # in one batch; cache and SQLite writes stay on this thread.
            missing = {}
            for key, png, value in prepared:
                if value is None:
                    missing.setdefault(key, png)

            if missing:
                if self.unavailable:
                    raise RuntimeError(self.unavailable)
                if self.service:
                    self.service.ensure()

                fresh = {}
                def save_responses(values):
                    for key, value in values.items():
                        if self.db is not None:
                            self.db.execute('INSERT OR REPLACE INTO responses VALUES (?,?)', (key, value))
                        else:
                            self.cache['responses'][key] = value
                    if self.db is not None:
                        self.db.commit()
                    elif values:
                        atomic_json(self.path, self.cache)

                items = list(missing.items())
                try:
                    values = recognize_lines([png for _, png in items], lang=lang)
                except Exception:
                    print(f'[line-ocr] batch request failed; restarting and retrying '
                          f'{len(items)} line(s)', flush=True)
                    if self.service:
                        self.service.recover()
                    values = recognize_lines([png for _, png in items], timeout=180, lang=lang)
                fresh.update((key, value) for (key, _), value in zip(items, values))
                save_responses(fresh)

            for key, png, value in prepared:
                if value is None:
                    value = fresh[key]
                candidates.append(strip_html(value))
        except Exception as ex:
            error = str(ex)
            self.unavailable = error
        block_key = hashlib.sha256(json.dumps({'original': original, 'images': keys}, ensure_ascii=False).encode()).hexdigest()
        reviewed = original
        if block_key in self.reviews:
            entry = self.reviews[block_key]
            if entry.get('original') != original or not entry.get('reason') or not isinstance(entry.get('replacement'), str):
                raise ValueError('Review does not match the original block')
            reviewed = entry['replacement']
            self.used_reviews.add(block_key)
        accepted = error is None and line_boundaries_are_usable(original, candidates)
        selected = align_to_recognized_lines(reviewed, candidates) if accepted else chunks
        self.cache['decisions'].append({'page': page.number+1, 'rects': [list(r) for r in rects],
                                       'block_key': block_key, 'image_keys': keys,
                                       'engine': engine,
                                       'original': original, 'before': chunks,
                                       'candidate': candidates, 'accepted': accepted, 'error': error,
                                       'reviewed': reviewed, 'selected': selected})
        if self.db is not None:
            self.db.execute('INSERT INTO decisions(value) VALUES (?)',
                            (json.dumps(self.cache['decisions'][-1], ensure_ascii=False),))
            self.db.commit()
        else:
            atomic_json(self.path, self.cache)
        if error and self.automatic:
            raise RuntimeError(f'Line OCR interrupted; cached crops retained: {error}')
        print(f'[line-ocr] p{page.number+1} {len(rects)} lines: '
              f'{"accepted" if accepted else "retained original"}' + (f' ({error})' if error else ''), flush=True)
        return selected


LATIN_TOKEN_RE = re.compile(r'[0-9A-Za-z]+')


def snap_out_of_latin_token(text, cut, window=3):
    """Move a line boundary off the inside of a Latin word or number.

    Printed Korean wraps between syllables, so Hangul is left alone, but a
    Latin word or number is never broken across lines in the source. Recognized
    text is approximate, so a boundary mapped from it can land a character or
    two inside such a token; this moves it to the nearer edge of that token.
    Only a near miss is corrected: a boundary deep inside a long token is what
    the recognizer actually reported, not rounding, so it is left as it is.
    """
    if cut <= 0 or cut >= len(text):
        return cut
    for token in LATIN_TOKEN_RE.finditer(text):
        if token.start() < cut < token.end():
            head, tail = cut - token.start(), token.end() - cut
            if min(head, tail) > window:
                return cut
            return token.start() if head <= tail else token.end()
        if token.start() >= cut:
            break
    return cut


def align_to_recognized_lines(original, candidates):
    """Transfer recognized line boundaries without copying recognition mistakes."""
    original = strip_html(original)
    offsets = [i for i,c in enumerate(original) if not c.isspace()]
    before = ''.join(original[i] for i in offsets)
    lines = [''.join(c.split()) for c in candidates]
    after = ''.join(lines)
    operations = SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
    result, start, boundary = [], 0, 0
    for line in lines[:-1]:
        boundary += len(line)
        mapped = 0
        for tag, a0, a1, b0, b1 in operations:
            if b0 <= boundary <= b1 and b1 > b0:
                mapped = round(a0 + (a1-a0)*(boundary-b0)/(b1-b0))
                break
        cut = offsets[mapped] if mapped < len(offsets) else len(original)
        cut = max(start, snap_out_of_latin_token(original, cut))
        result.append(original[start:cut].strip())
        start = cut
    result.append(original[start:].strip())
    return result


def build_toc(pages_md, filtered=True):
    raw = []
    for pno, md in enumerate(pages_md, 1):
        for line in md.splitlines():
            m = re.match(r"^(#{1,4})\s+(.+)$", line.strip())
            if not m:
                continue
            title = re.sub(r"[*_`]", "", m.group(2)).strip()
            if not (2 <= len(title) <= 80):
                continue
            if filtered and not TOC_KEEP.search(title):
                continue
            raw.append([len(m.group(1)), title, pno])
    toc, prev = [], 0
    for level, title, pno in raw:
        level = min(level, prev + 1)
        toc.append([level, title, pno])
        prev = level
    return toc


def overlay(
    pdf_path, pages_pruned, out_path, toc=None, paragraph_mode=False, debug=False,
    line_refiner=None, automatic=False
):
    doc = fitz.open(str(pdf_path))
    font, fontfile, fontobj = pick_font(doc)
    if not font:
        doc.close()
        raise RuntimeError("한글 글리프가 있는 폰트를 찾지 못했습니다.")
    print(f"[font] {font}" + (f" ({fontfile})" if fontfile else " (내장)"))
    fallback_fonts = []
    for name, path in (("ocrcjk", r"C:\Windows\Fonts\simsun.ttc"),
                       ("ocrsymbol", r"C:\Windows\Fonts\seguisym.ttf")):
        if Path(path).exists():
            fallback_fonts.append((name, fitz.Font(fontfile=path), path))
    fallback_fonts.append(("japan", fitz.Font("japan"), None))

    lines_ok = para_fallback = skipped = 0
    empty_boxes = textless_blocks = 0
    report = {'pages': [], 'mode':'automatic' if automatic else 'legacy'}
    count = lambda text: Counter(c for c in text if not c.isspace())

    for pno, pruned in enumerate(pages_pruned):
        if pno >= len(doc):
            continue
        page = doc[pno]
        existing = page.get_text()
        info = {'page':pno+1, 'warnings':[], 'expected_characters':count(existing),
                'existing_text':bool(existing.strip())}
        report['pages'].append(info)
        start_lines, start_empty, start_para, start_skip = lines_ok, empty_boxes, para_fallback, skipped
        start_textless = textless_blocks
        if automatic:
            print(f'[overlay] page {pno+1}/{len(doc)}', flush=True)
            if existing.strip():
                info['warnings'].append('existing_text_preserved')
                continue
        debug_rects = []
        if fontfile:
            page.insert_font(fontname=font, fontfile=fontfile)
        else:
            page.insert_font(fontname=font)

        blocks = _find_blocks(pruned)
        if not blocks:
            info['warnings'].append('no_ocr_text')
            continue

        size = _find_size(pruned)
        if size and size[0] > 0 and size[1] > 0:
            sx, sy = page.rect.width / size[0], page.rect.height / size[1]
        else:
            mx = max((b[2] for _, b in blocks), default=1) or 1
            my = max((b[3] for _, b in blocks), default=1) or 1
            sx, sy = page.rect.width / mx, page.rect.height / my

        for text, (x0, y0, x1, y1) in blocks:
            # U+FFFD is not a character the book contains; it is the marker for
            # one recognition could not represent. Carrying it into the text
            # layer adds something nobody can search for, and no font maps it
            # back to itself, so it also reads out as an unrelated glyph.
            if REPLACEMENT_CHAR in text:
                info.setdefault('undecodable_characters', 0)
                info['undecodable_characters'] += text.count(REPLACEMENT_CHAR)
                if 'undecodable_character_dropped' not in info['warnings']:
                    info['warnings'].append('undecodable_character_dropped')
                text = text.replace(REPLACEMENT_CHAR, '')
            info['expected_characters'].update(count(strip_html(text)))
            rect = fitz.Rect(x0 * sx, y0 * sy, x1 * sx, y1 * sy) & page.rect

            structured_text = text
            if HTML_TAG_RE.search(text) or '\\' in text:
                info['warnings'].append('complex_structure')
            text = strip_html(text)
            if not text.strip():
                # A block holding only an image or markup has nothing to insert.
                # That is not a failure, and counting it as one threw away a
                # finished 392-page book over one picture inside a table cell.
                textless_blocks += 1
                continue
            if rect.is_empty or rect.height < 1 or rect.width < 1:
                skipped += 1
                info['warnings'].append('insertion_failed')
                continue

            line_rects = [] if paragraph_mode else detect_lines(page, rect)

            if len(line_rects) >= 1:
                table = layout_table(page, rect, structured_text, line_rects, fontobj)
                if table:
                    line_rects, chunks = table
                else:
                    chunks = split_text(structured_text, line_rects, fontobj)
                if (line_refiner is not None and pno+1 in line_refiner.pages
                        and len(line_rects) > 1 and not HTML_TAG_RE.search(structured_text)
                        and not (automatic and ('\\' in structured_text or '$$' in structured_text))
                        and (automatic or re.search(r'[\uac00-\ud7a3]', text))):
                    chunks = line_refiner.refine(page, structured_text, line_rects, chunks)
                    decision = line_refiner.cache['decisions'][-1]
                    if not decision['accepted']:
                        info['warnings'].append('line_alignment_rejected')
                    if recognition_disagrees(text, decision['candidate']):
                        info['warnings'].append('ocr_disagreement_content_preserved')
                info['expected_characters'].subtract(count(text))
                info['expected_characters'].update(count(''.join(chunks)))
                for lr, chunk in zip(line_rects, chunks):
                    if not chunk.strip():
                        empty_boxes += 1
                        continue
                    try:
                        insert_invisible_line(page, chunk, lr, font, fontobj, fallback_fonts)
                        lines_ok += 1
                    except Exception as ex:
                        print(f"[overlay] p{pno + 1} insertion failed: {ex}", flush=True)
                        skipped += 1
                    if debug:
                        debug_rects.append(lr)
                continue

            # 줄을 못 찾으면 문단 통째로 (예전 방식)
            n = max(1, len(text))
            fs = (rect.get_area() / (0.68 * n)) ** 0.5
            fs = max(2.0, min(fs, rect.height, 40.0))
            rc = -1
            for _ in range(12):
                rc = page.insert_textbox(
                    rect * page.derotation_matrix, text, fontname=font, fontsize=fs,
                    render_mode=3, align=0, rotate=page.rotation
                )
                if rc >= 0:
                    break
                fs *= 0.8
                if fs < 1.5:
                    break
            if rc < 0:
                try:
                    page.insert_text(
                        fitz.Point(rect.x0, rect.y0 + 4) * page.derotation_matrix,
                        " ".join(text.split()),
                        fontname=font,
                        fontsize=3,
                        render_mode=3,
                        rotate=page.rotation,
                    )
                    para_fallback += 1
                except Exception:
                    skipped += 1
            else:
                para_fallback += 1

        for lr in debug_rects:
            page.draw_rect(lr * page.derotation_matrix, color=(1, 0, 0), width=0.4)
        info['inserted_lines'] = lines_ok-start_lines
        info['unassigned_boxes'] = empty_boxes-start_empty
        if textless_blocks > start_textless:
            info['textless_blocks'] = textless_blocks-start_textless
        if empty_boxes > start_empty:
            info['warnings'].append('unassigned_boxes')
        if para_fallback > start_para:
            info['warnings'].append('paragraph_fallback')
        if skipped > start_skip:
            info['warnings'].append('insertion_failed')
        info['warnings'] = sorted(set(info['warnings']))

    if toc:
        try:
            doc.set_toc(toc)
            print(f"[toc] {len(toc)} entries")
        except Exception as e:
            print(f"[toc] 실패: {e}")

    doc.save(str(out_path), garbage=3, deflate=True)
    doc.close()
    print(
        f"[overlay] {lines_ok} lines, {para_fallback} paragraph-fallback, "
        f"{skipped} skipped"
    )
    print(f"[overlay] {empty_boxes} boxes without assigned text (review required)")
    return report


# ------------------------------------------------------------------ main


def _validate_results(res, expected):
    rows = res.get("layoutParsingResults")
    if not isinstance(rows, list) or len(rows) != expected:
        raise RuntimeError(f"OCR page count mismatch: expected {expected}, got {len(rows) if isinstance(rows, list) else 'invalid'}")
    for row in rows:
        if not isinstance(row.get("prunedResult"), dict) or not isinstance(row.get("markdown", {}).get("text"), str):
            raise RuntimeError("Invalid OCR page result")
    return rows


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def auto_tag(args):
    """Name debug status and report files apart from a real run's.

    A border-drawing run is a diagnostic whose output is not the deliverable, so
    it must not overwrite the status and report describing the finished PDF.
    """
    return '_auto_debug' if args.debug_lines else '_auto'


def source_identity(src):
    digest = hashlib.sha256()
    with src.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    identity = {"sha256": digest.hexdigest(), "api": API, "options": API_OPTIONS}
    # Only recorded when it applies, so a book that needs no flattening keeps
    # the identity it already has and goes on reusing its cache.
    if needs_flattening(src):
        identity["ocr_input"] = f"flattened-{OCR_RENDER_ZOOM}x"
    return identity


def run_ocr(src: Path, batch: int, checkpoint=None, identity=None):
    pages_md, pages_pruned = [], []
    if checkpoint and checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if saved.get("identity") == identity and saved.get("batch") == batch:
            pages_md, pages_pruned = saved["markdown"], saved["pages"]
            if (not isinstance(pages_md,list) or not isinstance(pages_pruned,list)
                    or len(pages_md)!=len(pages_pruned)
                    or any(not isinstance(p,dict) for p in pages_pruned)
                    or any(not isinstance(m,str) for m in pages_md)):
                raise RuntimeError('Invalid OCR checkpoint; refusing a potentially shifted resume')
    t0 = time.time()
    if batch > 0:
        for start, chunk, total in split_pdf(src, batch):
            if len(pages_pruned)>total or (len(pages_pruned)!=total and len(pages_pruned)%batch):
                raise RuntimeError('Incomplete batch in OCR checkpoint; refusing a shifted resume')
            if start < len(pages_pruned):
                continue
            res = ocr_pdf(chunk)
            for r in _validate_results(res, min(batch, total - start)):
                pages_md.append(r["markdown"]["text"])
                pages_pruned.append(r["prunedResult"])
            if checkpoint:
                atomic_json(checkpoint, {"identity": identity, "batch": batch,
                                        "markdown": pages_md, "pages": pages_pruned})
            done = min(start + batch, total)
            el = time.time() - t0
            print(f"[{done}/{total}] {el:.1f}s  ({el / done:.2f}s/page)", flush=True)
    else:
        print("[ocr] 전체 전송 중... (완료까지 출력이 없습니다)", flush=True)
        if needs_flattening(src):
            with fitz.open(src) as doc:
                whole = _flattened(doc, 0, len(doc))
        else:
            whole = src.read_bytes()
        res = ocr_pdf(whole)
        for r in _validate_results(res, len(PdfReader(str(src)).pages)):
            pages_md.append(r["markdown"]["text"])
            pages_pruned.append(r["prunedResult"])
        n = max(1, len(pages_md))
        el = time.time() - t0
        print(f"[ocr] {n} pages in {el:.1f}s ({el / n:.2f}s/page)")
    return pages_md, pages_pruned


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--batch", type=int, default=10, help="N페이지씩 나눠 보내기 (0=통째로)"
    )
    ap.add_argument("--toc", action="store_true")
    ap.add_argument("--toc-all", action="store_true")
    ap.add_argument(
        "--paragraph",
        action="store_true",
        help="줄 검출 없이 문단 단위로 삽입 (예전 방식)",
    )
    ap.add_argument(
        "--debug-lines",
        action="store_true",
        help="검출된 줄 상자를 빨간 테두리로 그린다",
    )
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument('--fast', action='store_true', help='Skip automatic full-document line OCR and final verification')
    ap.add_argument('--line-ocr-pages', help='Opt-in crop OCR on these 1-based pages, e.g. 13,17,26 (or all)')
    ap.add_argument('--line-ocr-review', type=Path, help='Explicit image-bound, manually reviewed block corrections')
    ap.add_argument("--trust-cache", action="store_true", help="Allow legacy caches without source fingerprints")
    ap.add_argument("--repair-cache", action="store_true", help="Recheck cached pages for missing OCR text")
    ap.add_argument(
        "--no-repair",
        action="store_true",
        help="본문이 유실된 블록을 잘라 다시 읽는 단계를 건너뛴다",
    )
    ap.add_argument("--dump-structure", action="store_true")
    args = ap.parse_args()
    if args.batch < 0:
        ap.error("--batch must be nonnegative")
    if args.line_ocr_review and not args.line_ocr_pages:
        ap.error('--line-ocr-review requires --line-ocr-pages')

    src = Path(args.pdf).resolve()
    if not src.exists():
        sys.exit(f"파일 없음: {src}")

    automatic = not (args.fast or args.paragraph or args.dump_structure or args.line_ocr_pages)
    line_pages = set(range(1, len(PdfReader(str(src)).pages)+1)) if automatic else set()
    if args.line_ocr_pages:
        count = len(PdfReader(str(src)).pages)
        try:
            line_pages = set(range(1, count+1)) if args.line_ocr_pages == 'all' else {int(s) for s in args.line_ocr_pages.split(',')}
            if not line_pages or min(line_pages) < 1 or max(line_pages) > count or args.paragraph:
                raise ValueError()
        except ValueError:
            ap.error('--line-ocr-pages requires valid page numbers and cannot be used with --paragraph')

    out_dir = Path(args.out).resolve() if args.out else src.parent/'ocr_output'
    out_dir.mkdir(parents=True, exist_ok=True)

    import ocr_workflow as workflow
    status = out_dir/f'{src.stem}{auto_tag(args)}_status.json'
    with workflow.job_lock(out_dir/f'{src.stem}.lock'):
        if automatic:
            atomic_json(status, {'status':'running','source':str(src),'started':time.time()})
        try:
            process_pdf(args, src, out_dir, line_pages, automatic)
        except BaseException as ex:
            if automatic:
                atomic_json(status, {'status':'interrupted' if isinstance(ex,KeyboardInterrupt) else 'failed',
                                     'source':str(src),'error':str(ex),'resume':'Repeat the same command'})
            raise


def process_pdf(args, src, out_dir, line_pages, automatic):
    import ocr_workflow as workflow
    tag = auto_tag(args)
    service = workflow.Service(BASE, Path(__file__).parent) if automatic else None
    line_service = (workflow.Service(LINE_BASE, Path(__file__).parent,
                                     compose_service='paddleocr-line-api')
                    if line_pages else None)

    cache_json = out_dir / f"{src.stem}_pruned.json"
    md_path = out_dir / f"{src.stem}.md"
    meta_path = out_dir / f"{src.stem}_cache_meta.json"
    checkpoint = out_dir / f"{src.stem}_partial.json"
    identity = source_identity(src)
    t0 = time.time()

    if automatic and (cache_json.exists() or meta_path.exists()):
        try:
            valid = (meta_path.exists() and md_path.exists()
                     and json.loads(meta_path.read_text(encoding='utf-8')) == identity)
        except (ValueError, OSError):
            valid = False
        if not valid:
            fingerprint = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:20]
            cache_dir = out_dir/'.ocr_cache'/fingerprint
            cache_dir.mkdir(parents=True,exist_ok=True)
            cache_json, md_path = cache_dir/'pruned.json', cache_dir/'text.md'
            meta_path, checkpoint = cache_dir/'meta.json', cache_dir/'partial.json'
            print('[cache] unverified old cache retained; using isolated source-verified cache',flush=True)

    if cache_json.exists() and md_path.exists() and not args.no_cache:
        if meta_path.exists():
            if json.loads(meta_path.read_text(encoding="utf-8")) != identity:
                sys.exit("Cache does not match source/API settings; use --no-cache")
        elif not args.trust_cache:
            sys.exit("Legacy cache: verify the source and use --trust-cache, or use --no-cache")
        pages_pruned = json.loads(cache_json.read_text(encoding="utf-8"))
        pages_md = md_path.read_text(encoding="utf-8").split(MD_SEP)
        print(f"[cache] {len(pages_pruned)} pages  (--no-cache 로 재실행)")
    else:
        if service:
            service.ensure()
        try:
            h = requests.get(f"{BASE}/health", timeout=10).json()
            print(f"[health] {h.get('errorMsg')}")
        except Exception as e:
            sys.exit(f"API에 연결할 수 없습니다. docker compose up 확인. ({e})")
        pages_md, pages_pruned = run_ocr(src, args.batch, checkpoint, identity)
        if not args.no_repair:
            def save_repair_progress():
                atomic_json(checkpoint, {'identity':identity,'batch':args.batch,
                                        'markdown':pages_md,'pages':pages_pruned})
            repair_blocks(src, pages_pruned, on_update=save_repair_progress if automatic else None,
                          resume=automatic)
        for pi, pr in enumerate(pages_pruned):
            if any(e.get("repair_audit", {}).get("accepted") for e in pr.get("parsing_res_list", [])):
                pages_md[pi] = "\n\n".join(e.get("block_content", "") for e in pr["parsing_res_list"])
        md_path.write_text(MD_SEP.join(pages_md), encoding="utf-8")
        atomic_json(cache_json, pages_pruned)
        atomic_json(meta_path, identity)
        checkpoint.unlink(missing_ok=True)
        print(f"[md] {md_path}")

    if len(pages_pruned) != len(PdfReader(str(src)).pages):
        sys.exit("Cached OCR page count does not match the PDF")
    if args.repair_cache and not args.no_repair:
        repair_blocks(src, pages_pruned)
        for pi, pr in enumerate(pages_pruned):
            if any(e.get("repair_audit", {}).get("accepted") for e in pr.get("parsing_res_list", [])):
                pages_md[pi] = "\n\n".join(e.get("block_content", "") for e in pr["parsing_res_list"])
        atomic_json(cache_json, pages_pruned)
        md_path.write_text(MD_SEP.join(pages_md), encoding="utf-8")

    if args.dump_structure:
        p = out_dir / f"{src.stem}_structure.json"
        p.write_text(
            json.dumps(pages_pruned[0], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("최상위 키:", list(pages_pruned[0].keys()))
        blocks = _find_blocks(pages_pruned[0])
        print(f"탐지 블록 {len(blocks)}개, 이미지 크기 {_find_size(pages_pruned[0])}")
        for t, b in blocks[:5]:
            print("  ", [round(v, 1) for v in b], repr(t[:40]))
        print(f"덤프 -> {p}")
        return

    toc = None
    if args.toc or args.toc_all:
        toc = build_toc(pages_md, filtered=not args.toc_all)
        if not toc:
            print("[toc] 조건에 맞는 헤딩 없음 (--toc-all 시도)")

    suffix = "_debug" if args.debug_lines else "_searchable"
    refiner = None
    if line_pages:
        suffix = ('_auto' if automatic else '_line') + suffix
        refiner = LineRefiner(out_dir / f'{src.stem}_line_ocr.json', line_pages, args.line_ocr_review,
                              automatic=automatic, service=line_service)
    pdf_path = out_dir / f"{src.stem}{suffix}.pdf"
    temporary_pdf = out_dir/f'{src.stem}{suffix}.partial.pdf' if automatic else pdf_path
    try:
        report = overlay(src, pages_pruned, temporary_pdf, toc=toc,
                         paragraph_mode=args.paragraph, debug=args.debug_lines,
                         line_refiner=refiner, automatic=automatic)
        if automatic:
            workflow.verify(src, temporary_pdf, report, debug=args.debug_lines)
            report['source'] = str(src)
            report['output'] = str(pdf_path)
            report['source_identity'] = identity
            report['elapsed_seconds'] = time.time()-t0
            atomic_json(out_dir/f'{src.stem}{tag}_report.json', report)
            workflow.write_summary(out_dir/f'{src.stem}{tag}_report.md', report)
            if report['validation_failed']:
                raise RuntimeError('Output validation failed; previous final PDF retained. See auto_report.json')
            temporary_pdf.replace(pdf_path)
            atomic_json(out_dir/f'{src.stem}{tag}_status.json',
                        {'status':report['status'],'output':str(pdf_path),'review_pages':report['review_pages']})
    finally:
        if refiner:
            refiner.close()
    if refiner and set(refiner.reviews)-refiner.used_reviews:
        print('[line-ocr] WARNING: some review entries did not match these page images')
    print(f"[pdf] {pdf_path}")
    print(f"[done] {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
