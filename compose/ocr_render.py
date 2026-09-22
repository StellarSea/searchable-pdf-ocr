"""Pixel-preserving source rendering, line detection, and table geometry. All boxes use PDF points unless stated."""
import hashlib
import json
from pathlib import Path
import numpy as np
import pymupdf as fitz
from ocr_text import _TableParser, _assign_units, _split_words, strip_html, split_text


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

# Oversized scan PDFs sometimes store image coordinates as points. Scale only
# physical-size thresholds, never the render resolution or relative ink ratios.
REFERENCE_PAGE_LONG_EDGE = 792.0

OVERSIZED_SCAN_LONG_EDGE = 1600.0

# Bind derived boxes to implementation as well as runtime thresholds. A forgotten
# manual version bump cannot reuse old geometry after an algorithm edit.
_DETECTION_CODE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

def page_coordinate_scale(page):
    """Return the multiplier for physical-size thresholds on oversized scans.

    Normal and large-format PDFs keep their native point geometry. The scale is
    only applied once a page is too large to be a plausible book page in points;
    at that point the coordinates are treated like uniformly enlarged scan
    pixels. Relative image thresholds remain unchanged.
    """
    longest = max(float(page.rect.width), float(page.rect.height))
    if longest < OVERSIZED_SCAN_LONG_EDGE:
        return 1.0
    return longest / REFERENCE_PAGE_LONG_EDGE

def _long_runs(mask, length):
    """가로로 length 이상 이어지는 True 런만 남긴다 (1xL 열림 연산).

    표/박스 테두리를 글자와 가르는 유일하게 믿을 만한 신호가 획 길이다.
    잉크 비율로는 못 가른다. 밴드는 줄의 타이트한 잉크 범위라서 d, l, P 같은
    글자 세로획이 밴드 높이를 그대로 채우기 때문이다.

    Boolean runs are found in bounded row chunks. False padding separates rows,
    including transposed/strided input; no full-image int32 prefix arrays are needed.
    """
    h, n = mask.shape
    if length < 2 or n < length:
        return np.zeros_like(mask)
    if mask.dtype != np.bool_ or not isinstance(length, (int, np.integer)):
        return _long_runs_numeric(mask, length)
    result = np.zeros((h, n), dtype=bool)
    # At most about 1 Mi cells, or one complete row for exceptionally wide input.
    rows = max(1, 1048576 // (n + 2))
    for first in range(0, h, rows):
        batch = mask[first:first + rows]
        padded = np.zeros((len(batch), n + 2), dtype=bool)
        padded[:, 1:-1] = batch
        flat = padded.ravel()
        edges = np.flatnonzero(flat[1:] != flat[:-1]) + 1
        starts, ends = edges[::2], edges[1::2]
        keep = ends - starts >= length
        starts, ends = starts[keep], ends[keep]
        if len(starts) < 4096:
            opened = np.zeros(len(flat), dtype=bool)
            for start, end in zip(starts, ends):
                opened[start:end] = True
        else:
            # Disjoint runs alternate +1/-1: cumulative values are only 0 or 1,
            # so int8 is exact even for dense patterns with many intervals.
            opened = np.zeros(len(flat), dtype=np.int8)
            opened[starts] = 1
            opened[ends] = -1
            np.cumsum(opened, dtype=np.int8, out=opened)
        result[first:first + rows] = opened.reshape(padded.shape)[:, 1:-1]
    return result


def _long_runs_numeric(mask, length):
    """Preserve the original arithmetic for legacy non-Boolean callers."""
    # 침식: 길이 length 창이 전부 True인 시작 위치
    c = np.pad(np.cumsum(mask, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
    ero = (c[:, length:] - c[:, :-length]) == length
    # 팽창: 그 창들이 덮는 칸을 되살린다
    m = ero.shape[1]
    ce = np.pad(np.cumsum(ero, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
    j = np.arange(mask.shape[1])
    hi = np.minimum(j + 1, m)
    lo = np.maximum(j - length + 1, 0)
    return (ce[:, hi] - ce[:, lo]) > 0

class PageRaster:
    """One immutable source-page display list, not a full-page bitmap.

    Construct before overlay writes. source_sha256 must identify the document
    this page was loaded from; without it only in-memory render reuse is enabled.
    """

    def __init__(self, page, source_sha256=None, detection_cache=None):
        self.display_list = page.get_displaylist(annots=True)
        self.detection_cache = detection_cache
        self.identity = None
        if source_sha256:
            self.identity = {
                'version': 'source-crop-v1', 'source': source_sha256,
                'page': page.number, 'rect': list(page.rect),
                'cropbox': list(page.cropbox), 'mediabox': list(page.mediabox),
                'rotation': page.rotation, 'renderer': list(fitz.version),
                'aa': fitz.TOOLS.show_aa_level(), 'annots': True,
            }

    def get_pixmap(self, **kwargs):
        return self.display_list.get_pixmap(**kwargs)

    def crop_key(self, crop, settings):
        if self.identity is None:
            return None
        value = {'page': self.identity, 'crop': list(crop), 'zoom': ZOOM,
                 'colorspace': 'RGB', 'alpha': False, 'settings': settings}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def detection_key(self, rect):
        if self.identity is None:
            return None
        settings = {name: value for name, value in globals().items()
                    if name.isupper() and not name.startswith('_')
                    and isinstance(value, (int, float))}
        value = {'version': 'line-detection-v1', 'code': _DETECTION_CODE_SHA,
                 'page': self.identity, 'rect': list(rect), 'settings': settings,
                 'colorspace': 'GRAY', 'alpha': False}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

def detect_lines(page, rect, raster=None):
    """문단 영역을 렌더링해 가로 투영으로 실제 글자 줄 상자를 찾는다."""
    cache = raster.detection_cache if raster is not None else None
    key = raster.detection_key(rect) if cache is not None else None
    if key is not None:
        cached = cache.detection(key)
        if cached is not None:
            return [fitz.Rect(box) for box in cached]
    boxes = _detect_lines(page, rect, raster)
    if boxes is not None and key is not None:
        cache.save_detection(key, boxes)
    return boxes if boxes is not None else []


def _detect_lines(page, rect, raster=None):
    """None means transient render failure; [] means a successful empty check."""
    if rect.width < 4 or rect.height < 4:
        return []
    try:
        pix = (raster or page).get_pixmap(
            matrix=fitz.Matrix(ZOOM, ZOOM),
            clip=rect,
            colorspace=fitz.csGRAY,
            alpha=False,
        )
    except Exception:
        return None
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

    coordinate_scale = page_coordinate_scale(page)

    # 표/박스 테두리를 형태학적 열림으로 먼저 걷어낸다. 박스는 위아래 가로선이
    # 상자 폭을 통째로 채워서 셀 안 모든 열에 잉크를 남긴다. 그러면 단어 사이
    # 공백이 물리적으로 사라져 어떤 간격 기준으로도 글자 범위를 되찾을 수 없다.
    rule_len = int(RULE_MIN_LEN_PT * coordinate_scale * ZOOM)
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
    if pix.height >= 3 * MIN_LINE_PT * coordinate_scale * ZOOM:
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
    gap_px = GAP_MERGE_PT * coordinate_scale * ZOOM
    merged = []
    for b in bands:
        if merged and b[0] - merged[-1][1] <= gap_px:
            merged[-1][1] = b[1]
        else:
            merged.append(b)

    min_px = MIN_LINE_PT * coordinate_scale * ZOOM
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
        speck_w = SPECK_W * coordinate_scale
        solid = [q for q in runs if q[-1] + 1 - q[0] >= speck_w]
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

def layout_table(page, rect, text, rects, fontobj, raster=None):
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
    pix = (raster or page).get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=rect,
                          colorspace=fitz.csGRAY, alpha=False)
    ink = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < DARK
    # Remove grid lines over the whole table, not inside a tight one-line cell:
    # inside a cell, ordinary stems can span its entire height.
    coordinate_scale = page_coordinate_scale(page)
    hr = _long_runs(ink, int(RULE_MIN_LEN_PT * coordinate_scale * ZOOM))
    vr = _long_runs(ink.T, int(RULE_MIN_LEN_PT * coordinate_scale * ZOOM)).T
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
