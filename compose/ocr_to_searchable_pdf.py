#!/usr/bin/env python3
"""Searchable-PDF job orchestration and legacy import facade.

Entry point: ``python run.py <PDF>``. Automatic mode uses document OCR for
content, source pixels for boxes, and line OCR only as an alignment guide.
Pure algorithms live in ocr_text/schema/render/fonts; HTTP and persistence
live in ocr_api/storage. See docs/ARCHITECTURE.md before changing contracts.
"""

import hashlib
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, ExitStack, closing
import math
from pathlib import Path
from ocr_artifacts import resolve_text
from ocr_gpu_prefetch import GPUPrefetch
from ocr_document_prefetch import document_results
from ocr_bookmarks import build_plan, apply_plan, verify_bookmarks, load_review
from ocr_repair_prefetch import RepairPrefetch
from ocr_repair_prepare import RepairCPUPreparer, is_repair_entry, repair_rect

import requests

# Compatibility exports: old tools may keep importing this entry point.
from ocr_text import (
    HTML_TAG_RE,
    _TextParser,
    text_units,
    searchable_math,
    strip_html,
    HANGUL_RE,
    CJK_RE,
    line_language,
    TOC_KEEP,
    split_text,
    _assign_units,
    _TableParser,
    _split_words,
    _split_korean,
    repair_is_safe,
    line_refinement_is_safe,
    recognition_disagrees,
    line_boundaries_are_usable,
    LATIN_TOKEN_RE,
    snap_out_of_latin_token,
    align_to_recognized_lines,
    build_toc,
)

from ocr_schema import (
    BLOCK_LIST_KEYS,
    TEXT_KEYS,
    BBOX_KEYS,
    SIZE_KEYS,
    _norm_bbox,
    _find_blocks,
    _find_size,
    apply_layout_review,
)

from ocr_render import (
    ZOOM,
    DARK,
    MIN_INK,
    MIN_LINE_PT,
    GAP_MERGE_PT,
    RULE_FRAC,
    RULE_WEAK_FRAC,
    GRAPHIC_H_RATIO,
    GRAPHIC_INK_RATIO,
    CELL_GAP_RATIO,
    CELL_MIN_W_RATIO,
    RULE_MIN_LEN_PT,
    DEBRIS_H_RATIO,
    RULE_ROW_FRAC,
    SPECK_W,
    DITHER_RATE,
    REFERENCE_PAGE_LONG_EDGE,
    OVERSIZED_SCAN_LONG_EDGE,
    page_coordinate_scale,
    _long_runs,
    PageRaster,
    detect_lines,
    layout_table,
)

from ocr_fonts import (
    FONT_CANDIDATES,
    pick_font,
    fit_size,
    _INSTALLED_FONTS,
    _FONT_FOR_CODEPOINT,
    font_for_codepoint,
    insert_invisible_line,
    insert_invisible_paragraph,
    repair_generated_font_unicode,
)

from ocr_api import (
    BASE,
    API,
    LINE_BASE,
    LINE_API,
    LINE_REQUEST_LIMIT,
    LINE_ENGINE,
    LINE_ENGINE_KOREAN,
    API_OPTIONS,
    _call_api,
    recognize_lines,
)

from ocr_source import (
    OCR_RENDER_ZOOM,
    needs_flattening,
    _flattened,
    split_pdf,
    source_identity,
)

from ocr_modes import (
    auto_tag,
    is_automatic,
    run_tag,
    run_options,
    completed_status,
    build_parser,
)

from ocr_storage import (
    atomic_json,
    LineCache,
)


import pymupdf as fitz
import numpy as np
from pypdf import PdfReader
from ocr_layout import block_layout as shared_block_layout
from ocr_prepare import CPUPreparer


REPLACEMENT_CHAR = '�'


MD_SEP = "\n\n---PAGE---\n\n"


# 줄 검출 파라미터
REPAIR_RATIO = 0.4  # 글자 수가 기대치의 이 비율에 못 미치면 본문이 유실된 블록
REPAIR_MIN_CHARS = 200  # 단, 기대치가 이보다 큰 블록만 (작은 블록은 오탐)
CHAR_W_RATIO = 0.46  # 글자 폭 ~ 줄 높이의 이 배 (기대 글자 수 추정용)


# ---------------------------------------------------------------- API 호출


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


# ------------------------------------------------------------ 유실 블록 수선


def _expected_chars(rects):
    if not rects:
        return 0.0
    mh = float(np.median([r.height for r in rects]))
    return sum(r.width for r in rects) / max(0.1, CHAR_W_RATIO * mh)


def repair_blocks(pdf_path, pages_pruned, timeout=None, on_update=None, resume=False,
                  *, reuse_raster=True, recognize_image=None, prefetch=False,
                  cpu_workers=0, cpu_memory_mb=16384, cpu_stats=None, on_detection=None):
    """표로 오인돼 본문이 통째로 빠진 블록을 잘라 다시 읽는다.

    이메일/양식은 layout 검출이 table 로 잡는데, 표 인식이 격자 밖 문단을
    버려서 머리글만 남는 일이 있다 (274pt 높이 블록에 143자). 페이지 전체를
    useLayoutDetection=False 로 읽으면 본문은 살지만 좌표가 통째로 사라지고,
    같은 설정으로 블록만 잘라 보내면 본문과 주소가 모두 돌아온다.
    """
    recognize_image = recognize_image or ocr_image
    fixed = 0

    def failed(metadata, error):
        entry, pno, text, have, exp = metadata
        print(f"[repair] p{pno + 1} 실패: {error}", flush=True)
        entry['repair_error'] = str(error)
        if on_update:
            on_update()
            raise error

    def apply(metadata, got):
        nonlocal fixed
        entry, pno, text, have, exp = metadata
        entry.pop('repair_error', None)
        entry['repair_audit'] = {'original': text, 'candidate': got,
                                 'accepted': repair_is_safe(text, got)}
        if entry['repair_audit']['accepted']:
            print(f"[repair] p{pno + 1} {have} -> {len(strip_html(got))}자 "
                  f"(기대 {int(exp)})", flush=True)
            entry['block_content'] = got
            fixed += 1
        else:
            print(f"[repair] p{pno + 1} candidate rejected; original retained", flush=True)
        if on_update:
            on_update()

    with fitz.open(str(pdf_path)) as doc, RepairPrefetch(
            lambda png: recognize_image(png, timeout=timeout), apply, failed, enabled=prefetch) as feed, \
            RepairCPUPreparer(pdf_path, pages_pruned, workers=cpu_workers if reuse_raster else 0,
                              memory_mb=cpu_memory_mb, resume=resume, stats=cpu_stats) as prepared:
        for pno, pruned in enumerate(pages_pruned):
            feed.poll()
            if pno >= len(doc) or not isinstance(pruned, dict):
                continue
            entries = pruned.get("parsing_res_list")
            if not isinstance(entries, list):
                continue
            page = doc[pno]
            size = _find_size(pruned)
            if not size or size[0] <= 0 or size[1] <= 0:
                continue
            raster, raster_attempted = None, False
            for e in entries:
                feed.poll()
                if not is_repair_entry(e, resume):
                    continue
                text, raw = e.get("block_content"), e.get("block_bbox")
                have = len(strip_html(text))
                rect = repair_rect(page, raw, size)
                if rect is None:
                    continue
                if reuse_raster and not raster_attempted:
                    raster_attempted = True
                    try:
                        # Reuse only the immutable source drawing commands. No
                        # whole-page bitmap, new cache key, or changed pixels.
                        raster = PageRaster(page)
                    except Exception:
                        # Keep the original renderer / error handling available
                        # when a display list cannot be constructed.
                        raster = None
                rects = prepared.detect(page, rect, raster=raster, serial=detect_lines, on_wait=feed.poll)
                if on_detection is not None:
                    on_detection(page, rect, rects)
                if len(rects) < 3:
                    continue
                exp = _expected_chars(rects)
                if exp < REPAIR_MIN_CHARS or have >= REPAIR_RATIO * exp:
                    continue
                metadata = e, pno, text, have, exp
                try:
                    pix = (raster or page).get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=rect)
                    png = pix.tobytes("png")
                    del pix
                except Exception as ex:
                    feed.fail(metadata, ex)
                else:
                    feed.submit(metadata, png)
                    del png
    if prefetch:
        print(f"[repair-feed] {feed.stats['requests']} requests, "
              f"wait {feed.stats['wait_seconds']:.2f}s, errors {feed.stats['errors']}", flush=True)
    print(f"[repair] {fixed} blocks re-read")
    return fixed


# ------------------------------------------------------- 텍스트 레이어 삽입


class LineRefiner:
    """Coordinate crop OCR and conservative alignment; LineCache owns persistence.

    The recognizer supplies boundary hints, never replacement characters. Keep
    the image/block hashes unchanged so saved responses and reviews still match.
    """

    def __init__(self, path, pages, review_path=None, automatic=False, service=None,
                 source_sha256=None, *, recognize=None):
        self.path, self.pages = path, pages
        self.options = {'useLayoutDetection': False}
        self.reviews = json.loads(resolve_text(review_path).read_text(encoding='utf-8')) if review_path else {}
        self.store = LineCache(path, automatic=automatic)
        self.cache = self.store.data  # Compatibility view used by reports/tools.
        self.source_sha256 = source_sha256
        self.stats = {'rendered_crops': 0, 'early_cache_hits': 0}
        self.prefetch_stats = {'blocks': 0, 'crops': 0, 'peak_buffer_bytes': 0,
                               'errors': 0, 'prepare_seconds': 0.0, 'wait_seconds': 0.0}
        self.unavailable = None
        self.used_reviews = set()
        self.automatic, self.service = automatic, service
        self.recognize = recognize

    @property
    def db(self):
        """Legacy inspection handle; storage owns its lifetime."""
        return self.store.db

    def publish_decisions(self):
        """Replace the audit snapshot only after publishing a final PDF."""
        self.store.publish_decisions()

    def close(self):
        self.store.close()

    def response(self, key):
        return self.store.response(key)

    def indexed_response(self, crop_key):
        return self.store.indexed_response(crop_key)

    def index_crop(self, crop_key, image_key):
        self.store.index_crop(crop_key, image_key)

    def prefetch_images(self, page, original, rects, raster, budget=64*1024*1024):
        """Prepare at most one block of PNGs on the PDF/SQLite owning thread.

        The budget limits extra retained PNG bytes, not total process RSS. Large
        crops are left for normal rendering; no resolution/threshold is changed.
        No cache index is published speculatively. Recheck it when consuming.
        """
        images, retained = {}, 0
        padding = page_coordinate_scale(page)
        engine = LINE_ENGINE_KOREAN if line_language(original) else LINE_ENGINE
        settings = json.dumps({'api': LINE_API, 'engine': engine}, sort_keys=True)
        for rect in rects:
            crop = fitz.Rect(rect.x0-padding, rect.y0-0.7*padding,
                             rect.x1+padding, rect.y1+0.7*padding) & page.rect
            coordinates = tuple(crop)
            if coordinates in images:
                continue
            _, value = self.indexed_response(raster.crop_key(crop, settings))
            if value is not None:
                continue
            # Leave room for RGB samples + encoding work. MuPDF may still hold
            # decoded source images, as in the sequential renderer.
            estimate = (math.ceil(crop.width*ZOOM)+2)*(math.ceil(crop.height*ZOOM)+2)*8
            if estimate > budget-retained:
                break
            pix = raster.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=crop,
                                    colorspace=fitz.csRGB, alpha=False)
            png = pix.tobytes('png')
            if len(png) > budget-retained:
                break
            images[coordinates] = png
            retained += len(png)
        self.prefetch_stats['crops'] += len(images)
        self.prefetch_stats['peak_buffer_bytes'] = max(self.prefetch_stats['peak_buffer_bytes'], retained)
        return images

    def refine(self, page, original, rects, chunks, raster=None, *, executor=None,
               on_wait=None, preloaded_pngs=None, on_idle=None, gpu_feed=None):
        recognize = self.recognize or recognize_lines
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
            padding = page_coordinate_scale(page)
            settings = json.dumps({'api': LINE_API, 'engine': engine}, sort_keys=True)
            for rect in rects:
                crop = fitz.Rect(rect.x0-padding, rect.y0-0.7*padding,
                                 rect.x1+padding, rect.y1+0.7*padding) & page.rect
                crop_key = raster.crop_key(crop, settings) if raster else None
                key, value = self.indexed_response(crop_key)
                png = None
                if value is None:
                    png = preloaded_pngs.pop(tuple(crop), None) if preloaded_pngs is not None else None
                    if png is None:
                        pix = (raster or page).get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=crop,
                                                         colorspace=fitz.csRGB, alpha=False)
                        png = pix.tobytes('png')
                    self.stats['rendered_crops'] += 1
                    key = hashlib.sha256(settings.encode()+png).hexdigest()
                    value = self.response(key)
                    self.index_crop(crop_key, key)
                else:
                    self.stats['early_cache_hits'] += 1
                keys.append(key)
                prepared.append((key, png, value))

            if gpu_feed is not None:
                gpu_feed.wait([key for key, png, value in prepared if value is None], on_idle)
                prepared = [(key, png, self.response(key) if value is None else value)
                            for key, png, value in prepared]

            # One block can contain many detected lines. Send every missing crop
            # in one batch; cache and SQLite writes stay on this thread.
            missing = {}
            for key, png, value in prepared:
                if value is None:
                    missing.setdefault(key, png)

            if missing:
                # Release unused lookahead images before preparing another block.
                if preloaded_pngs is not None:
                    preloaded_pngs.clear()
                if self.unavailable:
                    raise RuntimeError(self.unavailable)
                if self.service:
                    self.service.ensure()

                fresh = {}

                items = list(missing.items())
                try:
                    if executor is None:
                        values = recognize([png for _, png in items], lang=lang)
                    else:
                        # Only bytes/strings enter the worker: never Page, Font,
                        # DisplayList, SQLite, cache writes or alignment decisions.
                        future = executor.submit(recognize, [png for _, png in items], lang=lang)
                        if on_wait is not None:
                            started = time.perf_counter()
                            try:
                                on_wait()
                            except Exception:
                                self.prefetch_stats['errors'] += 1
                                # Speculation is optional; normal handling retries
                                # this block later, without changing its text.
                            finally:
                                self.prefetch_stats['prepare_seconds'] += time.perf_counter()-started
                        waiting = time.perf_counter()
                        try:
                            if on_idle is not None:
                                while not future.done():
                                    on_idle()
                                    try:
                                        future.result(timeout=.1)
                                    except TimeoutError:
                                        # A completed request may itself raise
                                        # TimeoutError; result() below preserves
                                        # the existing HTTP retry behavior.
                                        pass
                            values = future.result()
                        finally:
                            self.prefetch_stats['wait_seconds'] += time.perf_counter()-waiting
                except Exception:
                    print(f'[line-ocr] batch request failed; restarting and retrying '
                          f'{len(items)} line(s)', flush=True)
                    if self.service:
                        self.service.recover()
                    values = recognize([png for _, png in items], timeout=180, lang=lang)
                fresh.update((key, value) for (key, _), value in zip(items, values))
                self.store.save_responses(fresh)

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
        if not accepted and reviewed != original:
            # A rejected OCR boundary must not silently discard an image-bound
            # human correction. Use the original layout as boundary hints while
            # retaining every character of the explicitly reviewed text.
            selected = align_to_recognized_lines(reviewed, chunks)
        self.store.append_decision({'page': page.number+1, 'rects': [list(r) for r in rects],
                                       'block_key': block_key, 'image_keys': keys,
                                       'engine': engine,
                                       'original': original, 'before': chunks,
                                       'candidate': candidates, 'accepted': accepted, 'error': error,
                                       'reviewed': reviewed, 'selected': selected})
        if error and self.automatic:
            raise RuntimeError(f'Line OCR interrupted; cached crops retained: {error}')
        outcome = ('accepted' if accepted else 'reviewed text; original boundaries'
                   if reviewed != original else 'retained original')
        print(f'[line-ocr] p{page.number+1} {len(rects)} lines: '
              f'{outcome}' + (f' ({error})' if error else ''), flush=True)
        return selected


def _block_layout(page, rect, structured_text, reviewed_line, paragraph_mode, fontobj, raster):
    """Shared exact layout path for normal and one-block lookahead preparation."""
    return shared_block_layout(page, rect, structured_text, reviewed_line, paragraph_mode,
                               fontobj, raster, detect=detect_lines, table=layout_table, split=split_text)


def _needs_line_ocr(refiner, pno, line_rects, structured_text, text, automatic):
    return (refiner is not None and pno+1 in refiner.pages and len(line_rects) > 1
            and not HTML_TAG_RE.search(structured_text)
            and not (automatic and ('\\' in structured_text or '$$' in structured_text))
            and (automatic or re.search(r'[\uac00-\ud7a3]', text)))


def overlay(
    pdf_path, pages_pruned, out_path, toc=None, paragraph_mode=False, debug=False,
    line_refiner=None, automatic=False, prefetch=True, prepared_pages=None,
    cpu_workers=0, cpu_memory_mb=16384, gpu_prefetch=False, bookmark_plan=None
):
    # Optional experimental injection; the CLI never supplies it. The callback
    # owns process lifecycle and returns plain data on this PDF-owning thread.
    if prepared_pages is not None and (not automatic or paragraph_mode):
        raise ValueError('Prepared pages require automatic line layout')
    pipeline = prefetch and automatic and line_refiner is not None and not paragraph_mode
    with fitz.open(str(pdf_path)) as doc, (ThreadPoolExecutor(max_workers=1, thread_name_prefix='ocr-http')
                                          if pipeline else nullcontext(None)) as http_executor, ExitStack() as preparation:
        first_new_xref = doc.xref_length()
        font, fontfile, fontobj = pick_font(doc)
        if not font:
            raise RuntimeError("한글 글리프가 있는 폰트를 찾지 못했습니다.")
        cpu_preparer = None
        if (prepared_pages is None and cpu_workers > 0 and automatic and not paragraph_mode
                and line_refiner is not None and line_refiner.source_sha256):
            cpu_preparer = preparation.enter_context(CPUPreparer(
                pdf_path, pages_pruned, line_refiner.store, line_refiner.source_sha256,
                {'fontfile': str(fontfile)} if fontfile else {'fontname': font},
                line_refiner.pages, workers=cpu_workers, memory_mb=cpu_memory_mb))
            prepared_pages = cpu_preparer
        gpu_feed = None
        if gpu_prefetch and pipeline and prepared_pages is not None:
            gpu_feed = preparation.enter_context(GPUPrefetch(
                line_refiner.store, http_executor, recognize_lines, pages_pruned,
                line_refiner.pages, line_refiner.service))
            if cpu_preparer is not None:
                cpu_preparer.on_ready = gpu_feed.offer
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
            boundary_repairs = [e['boundary_audit'] for e in pruned.get('parsing_res_list', [])
                                if 'boundary_audit' in e]
            if boundary_repairs:
                info['boundary_repairs'] = boundary_repairs
                if any(not entry['accepted'] for entry in boundary_repairs):
                    info['warnings'].append('boundary_recognition_rejected')
            start_lines, start_empty, start_para, start_skip = lines_ok, empty_boxes, para_fallback, skipped
            start_textless = textless_blocks
            if automatic:
                print(f'[overlay] page {pno+1}/{len(doc)}', flush=True)
                if existing.strip():
                    info['warnings'].append('existing_text_preserved')
                    continue
            debug_rects = []
            # Capture only source drawing commands, before adding invisible text.
            # Reusing them avoids rebuilding a display list for every crop without
            # allocating a giant page bitmap or changing crop pixels / resolution.
            raster = PageRaster(page, line_refiner.source_sha256 if line_refiner else None,
                                line_refiner.store if line_refiner else None)
            if fontfile:
                page.insert_font(fontname=font, fontfile=fontfile)
            else:
                page.insert_font(fontname=font)

            blocks = _find_blocks(pruned)
            reviewed_lines = {(b['block_content'], tuple(b['block_bbox']))
                              for b in pruned.get('parsing_res_list', [])
                              if b.get('block_label') == 'reviewed_line'}
            if pruned.get('layout_review_reason'):
                info['layout_review_reason'] = pruned['layout_review_reason']
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

            # Exactly one next-block buffer; never a queue of whole PDF pages.
            next_layout = {}
            prepared_blocks = prepared_pages(pno) if prepared_pages is not None else None
            if gpu_feed is not None and prepared_blocks is not None:
                gpu_feed.offer([(pno, prepared_blocks)])
            for block_index, (text, (x0, y0, x1, y1)) in enumerate(blocks):
                if cpu_preparer is not None:
                    cpu_preparer.pump(pno+1)
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

                reviewed_line = (text, (x0, y0, x1, y1)) in reviewed_lines
                cached_layout = next_layout.pop(block_index, None)
                next_layout.clear()
                if prepared_blocks is not None:
                    cached_layout = prepared_blocks.pop(block_index, None)
                if cached_layout is not None:
                    line_rects, chunks, preloaded_pngs = cached_layout
                else:
                    line_rects, chunks = _block_layout(page, rect, structured_text, reviewed_line,
                                                       paragraph_mode, fontobj, raster)
                    preloaded_pngs = None

                def prepare_next():
                    if prepared_blocks is not None:
                        return
                    if block_index+1 >= len(blocks):
                        return
                    upcoming, bbox = blocks[block_index+1]
                    upcoming = upcoming.replace(REPLACEMENT_CHAR, '')
                    # Complex/table paths stay in their original execution order.
                    if HTML_TAG_RE.search(upcoming) or '\\' in upcoming or '$$' in upcoming:
                        return
                    plain = strip_html(upcoming)
                    target = fitz.Rect(bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy) & page.rect
                    if not plain.strip() or target.is_empty or min(target.width, target.height) < 1:
                        return
                    reviewed = (plain, tuple(bbox)) in reviewed_lines
                    upcoming_rects, upcoming_chunks = _block_layout(
                        page, target, upcoming, reviewed, paragraph_mode, fontobj, raster)
                    # [] can also represent a transient render failure. Let the
                    # normal path retry instead of retaining speculative emptiness.
                    if not upcoming_rects:
                        return
                    images = (line_refiner.prefetch_images(page, upcoming, upcoming_rects, raster)
                              if _needs_line_ocr(line_refiner, pno, upcoming_rects, upcoming, plain, automatic)
                              else {})
                    next_layout[block_index+1] = (upcoming_rects, upcoming_chunks, images)
                    line_refiner.prefetch_stats['blocks'] += 1

                if len(line_rects) >= 1:
                    if _needs_line_ocr(line_refiner, pno, line_rects, structured_text, text, automatic):
                        chunks = line_refiner.refine(page, structured_text, line_rects, chunks, raster=raster,
                            executor=http_executor, on_wait=prepare_next if pipeline else None,
                            preloaded_pngs=preloaded_pngs,
                            on_idle=(lambda: cpu_preparer.pump(pno+1)) if cpu_preparer is not None else None,
                            gpu_feed=gpu_feed)
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

                try:
                    if insert_invisible_paragraph(page, text, rect, font, fontobj, fallback_fonts):
                        info['warnings'].append('paragraph_compacted')
                    para_fallback += 1
                except Exception as ex:
                    print(f"[overlay] p{pno + 1} insertion failed: {ex}", flush=True)
                    skipped += 1

            crop = page.cropbox_position
            shave = (crop.x, crop.y, crop.x, crop.y)
            for lr in debug_rects:
                # draw_rect puts its shape at media box coordinates, while these
                # rects live in page.rect space, which starts at the crop box. The
                # two are the same on an uncropped page and a couple of points apart
                # on a cropped one, which drew every border off the text it marks
                # even though the text itself was placed correctly.
                page.draw_rect((lr - shave) * page.derotation_matrix,
                               color=(1, 0, 0),
                               width=0.4 * page_coordinate_scale(page))
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

        if bookmark_plan is not None:
            report['bookmarks'] = apply_plan(doc, bookmark_plan)
            print(f"[bookmarks] {report['bookmarks']['inserted']} added, "
                  f"{len(report['bookmarks']['review'])} unresolved, "
                  f"{report['bookmarks']['preserved_existing']} existing preserved", flush=True)
        elif toc:
            try:
                doc.set_toc(toc)
                print(f"[toc] {len(toc)} entries")
            except Exception as e:
                print(f"[toc] 실패: {e}")

        repaired_maps = repair_generated_font_unicode(doc, first_new_xref)
        if repaired_maps:
            print(f'[font] corrected UTF-16 mappings in {repaired_maps} new font(s)', flush=True)
        doc.save(str(out_path), garbage=3, deflate=True)
        if line_refiner is not None:
            report['line_cache_stats'] = dict(line_refiner.stats)
            if line_refiner.source_sha256:
                report['detection_cache_stats'] = dict(line_refiner.store.detection_stats)
            if pipeline:
                report['prefetch_stats'] = dict(line_refiner.prefetch_stats)
        if cpu_preparer is not None:
            report['cpu_prepare_stats'] = dict(cpu_preparer.stats)
            stats = cpu_preparer.stats
            print(f"[cpu] prepared {stats['prepared_pages']}, cached {stats['cached_pages']}, "
                  f"serial-table {stats['serial_layout_pages']}, memory-skipped {stats['memory_skips']}, "
                  f"recoveries {len(stats['fallbacks'])}", flush=True)
        if gpu_feed is not None:
            gpu_feed.harvest()
            report['gpu_feed_stats'] = dict(gpu_feed.stats)
            print(f"[gpu-feed] {gpu_feed.stats['images']} crops / {gpu_feed.stats['requests']} requests, "
                  f"fallbacks {len(gpu_feed.stats['errors'])}", flush=True)
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


def run_ocr(src: Path, batch: int, checkpoint=None, identity=None, *, document_requests=1):
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
        feed_stats = {}
        with closing(document_results(split_pdf(src, batch), batch, len(pages_pruned),
                     ocr_pdf, requests=document_requests, stats=feed_stats)) as results:
            for start, res, total in results:
                for r in _validate_results(res, min(batch, total - start)):
                    pages_md.append(r["markdown"]["text"])
                    pages_pruned.append(r["prunedResult"])
                if checkpoint:
                    atomic_json(checkpoint, {"identity": identity, "batch": batch,
                                            "markdown": pages_md, "pages": pages_pruned})
                done = min(start + batch, total)
                el = time.time() - t0
                print(f"[{done}/{total}] {el:.1f}s  ({el / done:.2f}s/page)", flush=True)
        if document_requests > 1:
            print(f"[ocr-feed] {feed_stats['requests']} requests, "
                  f"peak {feed_stats['peak_pending']} pending, "
                  f"prepare {feed_stats['prepare_seconds']:.2f}s, "
                  f"wait {feed_stats['wait_seconds']:.2f}s, "
                  f"fallbacks {len(feed_stats['fallbacks'])}", flush=True)
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
    ap = build_parser()
    args = ap.parse_args()
    if args.batch < 0:
        ap.error("--batch must be nonnegative")
    if args.bookmark_review and not args.bookmarks:
        ap.error('--bookmark-review requires --bookmarks')
    if args.line_ocr_review and not args.line_ocr_pages:
        ap.error('--line-ocr-review requires --line-ocr-pages')

    src = Path(args.pdf).resolve()
    if not src.exists():
        sys.exit(f"파일 없음: {src}")

    automatic = is_automatic(args)
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
    status = workflow.document_artifact(out_dir, src.stem, f'{src.stem}{run_tag(args)}_status.json')
    with workflow.output_lock(out_dir, src.stem):
        workflow.compact_document(out_dir, src.stem)
        atomic_json(status, {'status':'running','source':str(src),'started':time.time()})
        try:
            process_pdf(args, src, out_dir, line_pages, automatic)
        except BaseException as ex:
            atomic_json(status, {'status':'interrupted' if isinstance(ex,KeyboardInterrupt) else 'failed',
                                 'source':str(src),'error':str(ex),'resume':'Repeat the same command'})
            raise


def process_pdf(args, src, out_dir, line_pages, automatic, *, bookmark_verifier=verify_bookmarks):
    import ocr_workflow as workflow
    tag = run_tag(args)
    artifact = lambda name: workflow.document_artifact(out_dir, src.stem, name)
    service = workflow.Service(BASE, Path(__file__).parent) if automatic else None
    line_service = (workflow.Service(LINE_BASE, Path(__file__).parent,
                                     compose_service='paddleocr-line-api')
                    if line_pages else None)

    cache_json = artifact(f"{src.stem}_pruned.json")
    md_path = artifact(f"{src.stem}.md")
    meta_path = artifact(f"{src.stem}_cache_meta.json")
    checkpoint = artifact(f"{src.stem}_partial.json")
    identity = source_identity(src)
    t0 = time.time()
    repair_cpu_runs = []

    if automatic and (cache_json.exists() or meta_path.exists()):
        try:
            valid = (meta_path.exists() and md_path.exists()
                     and json.loads(meta_path.read_text(encoding='utf-8')) == identity)
        except (ValueError, OSError):
            valid = False
        if not valid:
            fingerprint = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:20]
            cache_json, md_path = (artifact(f'.ocr_cache/{fingerprint}/{name}') for name in ('pruned.json', 'text.md'))
            meta_path, checkpoint = (artifact(f'.ocr_cache/{fingerprint}/{name}') for name in ('meta.json', 'partial.json'))
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
        pages_md, pages_pruned = run_ocr(src, args.batch, checkpoint, identity,
                                      document_requests=args.ocr_requests)
        if not args.no_repair:
            def save_repair_progress():
                atomic_json(checkpoint, {'identity':identity,'batch':args.batch,
                                        'markdown':pages_md,'pages':pages_pruned})
            repair_cpu_stats = {'phase': 'initial'}
            repair_cpu_runs.append(repair_cpu_stats)
            repair_blocks(src, pages_pruned, on_update=save_repair_progress if automatic else None,
                          resume=automatic, prefetch=not args.no_prefetch, cpu_workers=args.cpu_workers,
                          cpu_memory_mb=args.cpu_memory_mb, cpu_stats=repair_cpu_stats)
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
        repair_cpu_stats = {'phase': 'repair_cache'}
        repair_cpu_runs.append(repair_cpu_stats)
        repair_blocks(src, pages_pruned, prefetch=not args.no_prefetch, cpu_workers=args.cpu_workers,
                      cpu_memory_mb=args.cpu_memory_mb, cpu_stats=repair_cpu_stats)
        for pi, pr in enumerate(pages_pruned):
            if any(e.get("repair_audit", {}).get("accepted") for e in pr.get("parsing_res_list", [])):
                pages_md[pi] = "\n\n".join(e.get("block_content", "") for e in pr["parsing_res_list"])
        atomic_json(cache_json, pages_pruned)
        md_path.write_text(MD_SEP.join(pages_md), encoding="utf-8")

    if getattr(args, 'layout_review', None):
        pages_pruned = apply_layout_review(pages_pruned, args.layout_review, identity['sha256'])

    boundary_feed_stats = {}
    if not getattr(args, 'no_boundary_repair', False) and not args.dump_structure:
        import ocr_boundary
        pages_pruned = ocr_boundary.repair(src, pages_pruned,
            artifact(f'{src.stem}_boundary_cache.json'), identity, service=service, call_api=_call_api,
            prefetch=not args.no_prefetch, stats=boundary_feed_stats)
        for index, page in enumerate(pages_pruned):
            pages_md[index] = '\n\n'.join(e.get('block_content', '')
                                         for e in page.get('parsing_res_list', []))

    if getattr(args, 'layout_review', None):
        for index, page in enumerate(pages_pruned):
            if page.get('layout_review_reason'):
                pages_md[index] = '\n'.join(b['block_content'] for b in page['parsing_res_list'])

    if args.dump_structure:
        p = artifact(f"{src.stem}_structure.json")
        p.write_text(
            json.dumps(pages_pruned[0], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("최상위 키:", list(pages_pruned[0].keys()))
        blocks = _find_blocks(pages_pruned[0])
        print(f"탐지 블록 {len(blocks)}개, 이미지 크기 {_find_size(pages_pruned[0])}")
        for t, b in blocks[:5]:
            print("  ", [round(v, 1) for v in b], repr(t[:40]))
        print(f"덤프 -> {p}")
        atomic_json(artifact(f'{src.stem}{tag}_status.json'),
                    completed_status(args, identity, p))
        return

    toc = None
    bookmark_plan = (build_plan(pages_pruned, load_review(getattr(args, 'bookmark_review', None), identity, pages_pruned))
                     if getattr(args, 'bookmarks', False) else None)
    if bookmark_plan is None and (args.toc or args.toc_all):
        toc = build_toc(pages_md, filtered=not args.toc_all)
        if not toc:
            print("[toc] 조건에 맞는 헤딩 없음 (--toc-all 시도)")

    suffix = "_debug" if args.debug_lines else "_searchable"
    if args.paragraph:
        suffix = '_paragraph' + suffix
    refiner = None
    if line_pages:
        suffix = ('_auto' if automatic else '_line') + suffix
        refiner = LineRefiner(artifact(f'{src.stem}_line_ocr.json'), line_pages, args.line_ocr_review,
                              automatic=automatic, service=line_service,
                              source_sha256=identity['sha256'])
    pdf_path = artifact(f"{src.stem}{suffix}.pdf")
    temporary_pdf = artifact(f'{src.stem}{suffix}.partial.pdf') if automatic or bookmark_plan is not None else pdf_path
    try:
        report = overlay(src, pages_pruned, temporary_pdf, toc=toc,
                         paragraph_mode=args.paragraph, debug=args.debug_lines,
                         line_refiner=refiner, automatic=automatic, prefetch=not args.no_prefetch,
                         cpu_workers=args.cpu_workers, cpu_memory_mb=args.cpu_memory_mb,
                         gpu_prefetch=not args.no_gpu_prefetch,
                         **({'bookmark_plan': bookmark_plan} if bookmark_plan is not None else {}))
        if bookmark_plan is not None:
            bookmark_verifier(src, temporary_pdf, report['bookmarks'])
        if automatic:
            verification_stats = {}
            workflow.verify(src, temporary_pdf, report, debug=args.debug_lines,
                            workers=min(args.cpu_workers, getattr(args, 'verify_workers', 8)),
                            memory_mb=args.cpu_memory_mb, stats=verification_stats)
            report['verification_stats'] = verification_stats
            if bookmark_plan is not None and report['bookmarks']['review']:
                if report['status'] == 'completed':
                    report['status'] = 'completed_with_warnings'
            report['repair_cpu_stats'] = repair_cpu_runs
            report['boundary_feed_stats'] = boundary_feed_stats
            report['source'] = str(src)
            report['output'] = str(pdf_path)
            report['source_identity'] = identity
            report['elapsed_seconds'] = time.time()-t0
            report['line_decisions'] = refiner.cache['decisions'] if refiner else []
            if report['validation_failed']:
                failed_report = artifact(f'{src.stem}{tag}_failed_report.json')
                atomic_json(failed_report, report)
                workflow.write_summary(artifact(f'{src.stem}{tag}_failed_report.md'), report)
                final_state = 'previous final PDF retained' if pdf_path.exists() else 'no final PDF published'
                raise RuntimeError(f'Output validation failed; {final_state}. Report: {failed_report}')
            temporary_pdf.replace(pdf_path)
            # Pin the audit to the exact derived layout used by this PDF. The
            # immutable name also keeps a failed rerun from changing old audits.
            layout_digest = hashlib.sha256(json.dumps(pages_pruned, sort_keys=True,
                                             ensure_ascii=False).encode()).hexdigest()[:20]
            layout_path = artifact(f'{src.stem}{tag}_layout_{layout_digest}.json')
            atomic_json(layout_path, pages_pruned)
            report['layout_cache'] = str(layout_path)
            atomic_json(artifact(f'{src.stem}{tag}_report.json'), report)
            workflow.write_summary(artifact(f'{src.stem}{tag}_report.md'), report)
            if refiner and not args.debug_lines:
                refiner.publish_decisions()
        if not automatic and bookmark_plan is not None:
            temporary_pdf.replace(pdf_path)
            atomic_json(artifact(f'{src.stem}{tag}_bookmarks.json'),
                        {'source_identity': identity, **report['bookmarks']})
        atomic_json(artifact(f'{src.stem}{tag}_status.json'),
                    completed_status(args, identity, pdf_path, report if automatic else None))
    finally:
        if refiner:
            refiner.close()
    if refiner and set(refiner.reviews)-refiner.used_reviews:
        print('[line-ocr] WARNING: some review entries did not match these page images')
    print(f"[pdf] {pdf_path}")
    print(f"[done] {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
