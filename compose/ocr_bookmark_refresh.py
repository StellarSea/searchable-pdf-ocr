"""Offline bookmark-only refresh with exact page-object and full-page verification."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import uuid

import pymupdf as fitz

from ocr_artifacts import resolve_text
from ocr_bookmarks import build_plan, apply_plan, verify_bookmarks, load_review, layout_hash
from ocr_storage import atomic_json
from ocr_workflow import output_lock, find_artifact, document_artifact, write_summary


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def stamp(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns


def outline_objects(doc):
    refs = {row[3]['xref'] for row in doc.get_toc(simple=False)}
    catalog = doc.pdf_catalog()
    kind, value = doc.xref_get_key(catalog, 'Outlines')
    if kind == 'xref':
        refs.add(int(value.split()[0]))
    return refs


def verify_content(source, candidate):
    """No original object may change except the outline tree/catalog reference.

    Incremental save keeps object identities stable. Compare every other object
    and raw stream byte-for-byte, then all page text/boxes and 72-dpi pixels.
    This proves preservation of the existing OCR, independently of its accuracy.
    """
    start = time.monotonic()
    with fitz.open(source) as before, fitz.open(candidate) as after:
        if len(before) != len(after):
            raise RuntimeError('Bookmark refresh changed page count')
        allowed = outline_objects(before)
        for xref in range(1, before.xref_length()):
            if xref in allowed:
                continue
            if xref == before.pdf_catalog():
                keys = set(before.xref_get_keys(xref)) | set(after.xref_get_keys(xref))
                if any(before.xref_get_key(xref, k) != after.xref_get_key(xref, k) for k in keys-{'Outlines'}):
                    raise RuntimeError('Bookmark refresh changed document catalog data')
                continue
            if before.xref_object(xref) != after.xref_object(xref):
                raise RuntimeError(f'Bookmark refresh changed non-outline object {xref}')
            if before.xref_is_stream(xref):
                if before.xref_stream_raw(xref) != after.xref_stream_raw(xref):
                    raise RuntimeError(f'Bookmark refresh changed source stream {xref}')
        for index in range(len(before)):
            a, b = before[index], after[index]
            if a.get_text('rawdict') != b.get_text('rawdict'):
                raise RuntimeError(f'Bookmark refresh changed text or character positions on page {index+1}')
            if (a.rect, a.cropbox, a.mediabox, a.rotation) != (b.rect, b.cropbox, b.mediabox, b.rotation):
                raise RuntimeError(f'Bookmark refresh changed page geometry on page {index+1}')
            old = hashlib.sha256(a.get_pixmap(dpi=72).samples).digest()
            new = hashlib.sha256(b.get_pixmap(dpi=72).samples).digest()
            if old != new:
                raise RuntimeError(f'Bookmark refresh changed pixels on page {index+1}')
            if (index+1) % 10 == 0 or index+1 == len(before):
                print(f'[bookmark-verify] {index+1}/{len(before)} pages: identical pixels/text/boxes', flush=True)
        return {'pages': len(before), 'non_outline_objects': before.xref_length()-1-len(allowed),
                'pixels_dpi': 72, 'all_pixels_equal': True, 'all_text_and_boxes_equal': True,
                'all_page_objects_and_streams_equal': True, 'seconds': round(time.monotonic()-start, 2)}


def refresh(pdf, review_path=None, *, content_verifier=verify_content):
    pdf = Path(pdf).resolve()
    suffix = '_auto_searchable.pdf'
    if not pdf.name.endswith(suffix):
        raise ValueError('Bookmark refresh requires an existing *_auto_searchable.pdf and its OCR report')
    stem, folder = pdf.name[:-len(suffix)], pdf.parent
    with output_lock(folder, stem):
        report_path = document_artifact(folder, stem, stem+'_auto_report.json')
        old_report = json.loads(find_artifact(folder, stem+'_auto_report.json').read_text(encoding='utf-8'))
        if Path(old_report['output']).resolve() != pdf:
            raise ValueError('OCR report output does not match this PDF')
        source = Path(old_report['source'])
        if file_hash(source) != old_report['source_identity']['sha256']:
            raise ValueError('Original source PDF changed since OCR')
        pages = json.loads(resolve_text(old_report['layout_cache']).read_text(encoding='utf-8'))
        recorded_layout = re.search(r'_layout_([0-9a-f]{20})\.json$', old_report['layout_cache'])
        if not recorded_layout or layout_hash(pages)[:20] != recorded_layout[1]:
            raise ValueError('Published OCR layout hash mismatch')
        if review_path is None:
            review_path = old_report.get('bookmark_review_path')
        corrections = load_review(review_path, old_report['source_identity'], pages)
        plan = build_plan(pages, corrections)
        old_bookmarks = old_report.get('bookmarks')
        replace = (old_bookmarks if old_bookmarks and old_bookmarks.get('inserted')
                   and not old_bookmarks.get('preserved_existing') else None)
        # The history directory is always a descendant of this explicit output
        # folder. Keep the old PDF and report; never clean up operational data.
        history = folder/'.ocr'/'bookmark_history'/stem/(time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8])
        history.mkdir(parents=True)
        before_pdf, candidate = history/'before.pdf', history/'candidate.pdf'
        original_stamp = stamp(pdf)
        shutil.copyfile(pdf, before_pdf)
        shutil.copyfile(pdf, candidate)
        atomic_json(history/'before_report.json', old_report)
        with fitz.open(candidate) as doc:
            if len(doc) != len(pages):
                raise ValueError('OCR layout and PDF page counts differ')
            result = apply_plan(doc, plan, previous_generated=replace)
            doc.saveIncr()
        verify_bookmarks(before_pdf, candidate, result)
        preservation = content_verifier(before_pdf, candidate)
        # Refuse publication if any independent writer changed the input.
        if stamp(pdf) != original_stamp or file_hash(pdf) != file_hash(before_pdf):
            raise RuntimeError('PDF changed during bookmark refresh; candidate not published')
        updated = copy.deepcopy(old_report)
        updated['bookmarks'] = result
        updated['bookmark_refresh'] = {'before_sha256': file_hash(before_pdf), 'output_sha256': file_hash(candidate),
            'backup': str(before_pdf), 'preservation': preservation, 'layout_sha256': layout_hash(pages)}
        if review_path:
            updated['bookmark_review_path'] = str(Path(review_path).resolve())
        if result['review'] and updated.get('status') == 'completed':
            updated['status'] = 'completed_with_warnings'
        atomic_json(history/'verified_report.json', updated)
        return _publish_verified(history)


def _publish_verified(history):
    """Caller owns the output lock. A saved verification is reusable only for
    the exact unchanged source, current output, report and candidate bytes."""
    updated = json.loads((history/'verified_report.json').read_text(encoding='utf-8'))
    old_report = json.loads((history/'before_report.json').read_text(encoding='utf-8'))
    pdf = Path(updated['output']).resolve()
    stem = pdf.name.removesuffix('_auto_searchable.pdf')
    folder = pdf.parent
    if not pdf.name.endswith('_auto_searchable.pdf') or history.parent != folder/'.ocr'/'bookmark_history'/stem:
        raise ValueError('Verified history is outside the expected document output directory')
    preservation = updated['bookmark_refresh']['preservation']
    if not updated['bookmarks'].get('verified') or not all(preservation.get(k) for k in (
            'all_pixels_equal', 'all_text_and_boxes_equal', 'all_page_objects_and_streams_equal')):
        raise ValueError('History does not contain successful preservation verification')
    candidate, before_pdf = history/'candidate.pdf', history/'before.pdf'
    if file_hash(candidate) != updated['bookmark_refresh']['output_sha256']:
        raise ValueError('Verified candidate changed')
    expected = updated['bookmark_refresh']['before_sha256']
    if file_hash(pdf) != expected or file_hash(before_pdf) != expected:
        raise ValueError('Current PDF or backup changed since verification')
    if file_hash(old_report['source']) != old_report['source_identity']['sha256']:
        raise ValueError('Source PDF changed since verification')
    current = json.loads(find_artifact(folder, stem+'_auto_report.json').read_text(encoding='utf-8'))
    if current != old_report:
        raise ValueError('Published OCR report changed since verification')
    candidate.replace(pdf)
    atomic_json(document_artifact(folder, stem, stem+'_auto_report.json'), updated)
    write_summary(document_artifact(folder, stem, stem+'_auto_report.md'), updated)
    state_path = find_artifact(folder, stem+'_auto_status.json')
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        state['status'] = updated['status']
        state.setdefault('options', {})['bookmarks'] = updated['bookmarks']['version']
        if updated.get('bookmark_review_path'):
            state['options']['bookmark_review'] = file_hash(updated['bookmark_review_path'])
        atomic_json(document_artifact(folder, stem, stem+'_auto_status.json'), state)
    result = updated['bookmarks']
    print(f"[bookmarks] {pdf.name}: {result['inserted']} added, {len(result['review'])} unresolved; backup {before_pdf}", flush=True)
    return updated


def publish_verified(history):
    history = Path(history).resolve()
    report = json.loads((history/'verified_report.json').read_text(encoding='utf-8'))
    pdf = Path(report['output']).resolve()
    stem = pdf.name.removesuffix('_auto_searchable.pdf')
    if history.parent != pdf.parent/'.ocr'/'bookmark_history'/stem:
        raise ValueError('Verified history is outside the expected document output directory')
    with output_lock(pdf.parent, stem):
        return _publish_verified(history)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf_or_folder', type=Path, nargs='?')
    parser.add_argument('--publish-verified', type=Path, help='Retry publication from an unchanged, fully verified history directory')
    parser.add_argument('--review', type=Path, help='Visually checked source/layout-bound bookmark corrections for one PDF')
    args = parser.parse_args()
    if args.publish_verified:
        if args.pdf_or_folder or args.review:
            parser.error('--publish-verified cannot be combined with a PDF or review')
        publish_verified(args.publish_verified)
        return
    if args.pdf_or_folder is None:
        parser.error('A PDF or output folder is required')
    target = args.pdf_or_folder.resolve()
    if target.is_dir():
        if args.review:
            parser.error('--review requires one PDF, not a folder')
        paths = sorted(target.glob('*_auto_searchable.pdf'))
    else:
        paths = [target]
    if not paths:
        parser.error('No completed PDF found')
    for path in paths:
        refresh(path, args.review)


if __name__ == '__main__':
    main()
