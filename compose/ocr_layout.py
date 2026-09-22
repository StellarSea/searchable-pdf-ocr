"""Shared exact block geometry for the PDF owner and isolated CPU workers."""
import pymupdf as fitz
from ocr_render import detect_lines, layout_table, page_coordinate_scale
from ocr_text import split_text, strip_html, HTML_TAG_RE, line_language
from ocr_schema import _find_blocks, _find_size
from ocr_api import LINE_API, LINE_ENGINE, LINE_ENGINE_KOREAN
import json


def block_layout(page, rect, text, reviewed, paragraph, font, raster,
                 detect=detect_lines, table=layout_table, split=split_text):
    boxes = [rect] if reviewed else [] if paragraph else detect(page, rect, raster=raster)
    if not boxes:
        return boxes, None
    cells = table(page, rect, text, boxes, font, raster=raster)
    return cells if cells else (boxes, split(text, boxes, font))


def page_blocks(page, pruned):
    blocks = _find_blocks(pruned)
    size = _find_size(pruned)
    if size and size[0] > 0 and size[1] > 0:
        sx, sy = page.rect.width/size[0], page.rect.height/size[1]
    else:
        sx = page.rect.width/(max((box[2] for _, box in blocks), default=1) or 1)
        sy = page.rect.height/(max((box[3] for _, box in blocks), default=1) or 1)
    reviewed = {(b['block_content'], tuple(b['block_bbox']))
                for b in pruned.get('parsing_res_list', []) if b.get('block_label') == 'reviewed_line'}
    for index, (text, bbox) in enumerate(blocks):
        text = text.replace('\ufffd', '')
        plain = strip_html(text)
        rect = fitz.Rect(bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy) & page.rect
        if plain.strip() and not rect.is_empty and min(rect.width, rect.height) >= 1:
            yield index, text, rect, (plain, tuple(bbox)) in reviewed


def needs_crops(text, boxes):
    return len(boxes) > 1 and not HTML_TAG_RE.search(text) and '\\' not in text and '$$' not in text


def crop_rect(page, box):
    padding = page_coordinate_scale(page)
    return fitz.Rect(box.x0-padding, box.y0-.7*padding, box.x1+padding, box.y1+.7*padding) & page.rect


def line_settings(text):
    engine = LINE_ENGINE_KOREAN if line_language(text) else LINE_ENGINE
    return json.dumps({'api': LINE_API, 'engine': engine}, sort_keys=True)
