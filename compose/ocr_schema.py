"""Read OCR response geometry and apply source-bound manual reviews without mutating raw input."""
import copy
import json
from pathlib import Path
import numpy as np


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

def apply_layout_review(pages, review_path, source_sha256):
    """Apply source-bound, visually reviewed lines without overwriting OCR caches.

    Unlike line OCR boundaries, these corrections can restore text outside the
    detector's original crop. Each replacement is a single physical line/cell in
    the original OCR image coordinates, and requires the exact original blocks.
    """
    from ocr_artifacts import resolve_text
    review = json.loads(resolve_text(review_path).read_text(encoding='utf-8'))
    if review.get('source_sha256') != source_sha256:
        raise ValueError('Layout review does not match the source PDF')
    result = copy.deepcopy(pages)
    seen = set()
    for entry in review.get('pages', []):
        number = entry.get('page')
        if type(number) is not int or not 1 <= number <= len(pages) or number in seen:
            raise ValueError('Invalid or repeated layout review page')
        seen.add(number)
        page = result[number - 1]
        if not entry.get('reason') or entry.get('original_blocks') != page.get('parsing_res_list'):
            raise ValueError('Layout review does not match the original OCR blocks')
        size = _find_size(page)
        replacements = entry.get('lines')
        if not size or not isinstance(replacements, list) or not replacements:
            raise ValueError('Layout review requires image size and reviewed lines')
        blocks = []
        for line in replacements:
            text, box = line.get('text'), line.get('bbox')
            if (not isinstance(text, str) or not text.strip() or '\n' in text
                    or not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (int, float) or not np.isfinite(v) for v in box)
                    or not 0 <= box[0] < box[2] <= size[0]
                    or not 0 <= box[1] < box[3] <= size[1]):
                raise ValueError('Invalid reviewed line text or bounding box')
            blocks.append({'block_content': text, 'block_bbox': box,
                           'block_label': 'reviewed_line'})
        page['parsing_res_list'] = blocks
        page['layout_review_reason'] = entry['reason']
    return result
