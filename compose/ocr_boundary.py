"""Pixel-based detection and audited repair of text clipped by layout boxes."""
import copy
import hashlib
import json
import re
from collections import Counter

import numpy as np
import pymupdf as fitz

import ocr_api
from ocr_render import _long_runs, page_coordinate_scale
from ocr_repair_prefetch import RepairPrefetch
from ocr_schema import _find_blocks, _find_size, _norm_bbox
from ocr_storage import BoundaryCache
from ocr_text import strip_html

VERSION = 'boundary-ink-v3'


def _runs(active, gap=0):
    points = np.flatnonzero(active)
    if not len(points):
        return []
    return [(int(p[0]), int(p[-1])+1) for p in
            np.split(points, np.flatnonzero(np.diff(points) > gap+1)+1)]


def expanded_box(ink, box, limit, neighbours=()):
    """Follow ink across all four edges, stopping at whitespace or other blocks.

    Coordinates are in the normalized analysis raster. A nearby unrelated word
    is not evidence: a text run must cross the current edge on the same band.
    """
    h, w = ink.shape
    x0, y0, x1, y1 = box
    lx, ly = limit
    ax, ay = max(0, int(x0-lx)), max(0, int(y0-ly))
    bx, by = min(w, int(x1+lx+1)), min(h, int(y1+ly+1))
    mask = ink[ay:by, ax:bx].copy()
    for neighbour in neighbours:
        r = fitz.Rect(neighbour) & fitz.Rect(ax, ay, bx, by)
        if not r.is_empty:
            mask[max(0,int(r.y0)-ay):min(by-ay,int(np.ceil(r.y1))-ay),
                 max(0,int(r.x0)-ax):min(bx-ax,int(np.ceil(r.x1))-ax)] = False
    # Rules are not characters crossing a block boundary.
    mask &= ~(_long_runs(mask, 80) | _long_runs(mask.T, 80).T)
    bounds = [float(x0), float(y0), float(x1), float(y1)]
    bands = _runs(mask.sum(axis=1) >= 3, gap=2)
    for a, b in bands:
        if b-a < 4 or b-a > 80 or b+ay < y0 or a+ay > y1:
            continue
        columns = mask[a:b].sum(axis=0) >= 2
        for left, right in _runs(columns, gap=max(2, int((b-a)*0.8))):
            left, right = left+ax, right+ax
            if left < x0-1 and right > x0+2:
                bounds[0] = min(bounds[0], left-2)
            if left < x1-2 and right > x1+1:
                bounds[2] = max(bounds[2], right+2)
    # Top/bottom cuts are found through a connected vertical projection. Limit
    # the gap so the next printed line is never consumed as an extension.
    ix0, ix1 = max(0,int(x0)-ax), min(bx-ax,int(np.ceil(x1))-ax)
    for top, bottom in _runs(mask[:, ix0:ix1].sum(axis=1) >= 3, gap=2):
        top, bottom = top+ay, bottom+ay
        if top < y0-1 and bottom > y0+2:
            bounds[1] = min(bounds[1], top-2)
        if top < y1-2 and bottom > y1+1:
            bounds[3] = max(bounds[3], bottom+2)
    bounds = [max(0,bounds[0]), max(0,bounds[1]), min(w,bounds[2]), min(h,bounds[3])]
    # Do not enlarge into the rectangle of another layout block, even if its
    # ink was masked during detection (e.g. an adjacent column).
    grown = fitz.Rect(bounds)
    original = fitz.Rect(box)
    if any((original & fitz.Rect(n)).get_area() <= 1
           and (grown & fitz.Rect(n)).get_area() > 1
           for n in neighbours):
        return list(box)
    return bounds


def preserves_content(old, new):
    """Permit recovered characters, but reject deletions and changed numbers."""
    before = ''.join(strip_html(old).split())
    after = ''.join(strip_html(new).split())
    if not before or not after or len(after) > max(len(before)*1.6, len(before)+32):
        return False
    remaining = iter(after)
    retained = all(char in remaining for char in before)
    numbers = lambda s: Counter(re.findall(r'\d+(?:[.,]\d+)*', s))
    return retained and not (numbers(before)-numbers(after))


def repair(source, pages, cache_path, identity, service=None, *, call_api=None,
           prefetch=False, stats=None):
    """Automatically check even cached OCR; store source-bound derived results.

    Original OCR caches are immutable here. Successful checks are cached per
    page, including pages with no cut edges. API failures remain retryable.
    Optional prefetch overlaps one HTTP call with owner-side preparation;
    completed-page checkpoints still precede the following HTTP dispatch.
    """
    call_api = call_api or ocr_api._call_api
    result = copy.deepcopy(pages)
    key = {'source': identity['sha256'], 'version': VERSION, 'api': ocr_api.API}
    cache = BoundaryCache(cache_path, key)
    deferred_checkpoint = None
    last_request_page = None

    def checkpoint():
        nonlocal deferred_checkpoint
        if deferred_checkpoint is not None:
            cache.save_page(*deferred_checkpoint)
            deferred_checkpoint = None

    def apply(metadata, response):
        entry, text, original_bbox, repaired_bbox, index, bi = metadata
        candidate = '\n'.join(t for row in response.get('layoutParsingResults', [])
                             for t,_ in _find_blocks(row.get('prunedResult', {})))
        accepted = preserves_content(text, candidate)
        entry['boundary_audit'] = {'original': text, 'candidate': candidate,
            'original_bbox': original_bbox, 'accepted': accepted, 'version': VERSION}
        entry['block_bbox'] = repaired_bbox
        entry.pop('block_polygon_points', None)
        if accepted:
            entry['block_content'] = candidate
        print(f'[boundary] p{index+1} block {bi}: expanded; '
              f'recognition {"accepted" if accepted else "rejected (review required)"}', flush=True)
        # Only a page whose preparation reached its end can be checkpointed.
        # A previous page's final HTTP result is committed before next dispatch.
        checkpoint()

    def failed(metadata, error):
        raise error

    feed = RepairPrefetch(lambda png: call_api(png, 1, options={'useLayoutDetection': False}),
                          apply, failed, enabled=prefetch)
    try:
        with fitz.open(source) as doc, feed:
            for index, pruned in enumerate(result):
                feed.poll()
                fingerprint = hashlib.sha256(json.dumps(pages[index], sort_keys=True,
                                          ensure_ascii=False).encode()).hexdigest()
                cached = cache.get(index, fingerprint)
                if cached is not None:
                    result[index] = cached
                    continue
                size = _find_size(pruned)
                entries = pruned.get('parsing_res_list', [])
                if not size or not entries:
                    continue
                page = doc[index]
                zoom = 2 / page_coordinate_scale(page)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom,zoom), colorspace=fitz.csGRAY)
                ink = np.frombuffer(pix.samples, np.uint8).reshape(pix.height,pix.width) < 185
                sx, sy = pix.width/size[0], pix.height/size[1]
                boxes = [_norm_bbox(e.get('block_bbox', [])) for e in entries]
                pixels = [[b[0]*sx,b[1]*sy,b[2]*sx,b[3]*sy] if b else None for b in boxes]
                for bi, (entry, box) in enumerate(zip(entries, pixels)):
                    feed.poll()
                    text = entry.get('block_content', '')
                    if box is None or not text.strip() or entry.get('block_label') == 'reviewed_line':
                        continue
                    neighbours = [b for j,b in enumerate(pixels) if j != bi and b is not None]
                    grown = expanded_box(ink, box, (pix.width*.15, pix.height*.025), neighbours)
                    if max(abs(a-b) for a,b in zip(grown,box)) < 2:
                        continue
                    rect = fitz.Rect(grown[0]/zoom,grown[1]/zoom,grown[2]/zoom,grown[3]/zoom) & page.rect
                    if service:
                        service.ensure()
                    # Keep PDF calls on the owner and preserve the exact crop.
                    crop = page.get_pixmap(matrix=fitz.Matrix(zoom,zoom), clip=rect)
                    png = crop.tobytes('png')
                    del crop
                    metadata = (entry, text, boxes[bi],
                                [grown[0]/sx,grown[1]/sy,grown[2]/sx,grown[3]/sy], index, bi)
                    feed.submit(metadata, png)
                    last_request_page = index
                    del png
                if feed.pending is not None and last_request_page == index:
                    # Prepare the following page while this final request runs.
                    # apply() saves this page before another HTTP request starts.
                    deferred_checkpoint = (index, fingerprint, pruned)
                else:
                    # A no-request page must not checkpoint ahead of a pending
                    # prior page. Its CPU preparation has already overlapped.
                    feed.finish()
                    cache.save_page(index, fingerprint, pruned)
    finally:
        if stats is not None:
            stats.update(feed.stats)
    cache.finish()
    return result
