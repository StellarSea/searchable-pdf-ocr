"""PDF-owner bookmark writes, review validation and saved-destination checks."""
import copy
import hashlib
import json

import pymupdf as fitz

from ocr_artifacts import resolve_text
from ocr_bookmark_match import VERSION, build_plan


def layout_hash(pages):
    return hashlib.sha256(json.dumps(pages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_review(path, source_identity, pages):
    """Read visually verified bookmark-only corrections bound to source/layout."""
    if not path:
        return []
    review = json.loads(resolve_text(path).read_text(encoding='utf-8'))
    if review.get('source_sha256') != source_identity['sha256'] or review.get('layout_sha256') != layout_hash(pages):
        raise ValueError('Bookmark review does not match source PDF and OCR layout')
    if not isinstance(review.get('corrections'), list):
        raise ValueError('Bookmark review requires a corrections list')
    return review['corrections']


def apply_plan(doc, plan, previous_generated=None):
    """Apply on the PDF-owning thread. Existing outlines are preserved wholesale."""
    report = copy.deepcopy(plan)
    existing = doc.get_toc(simple=False)
    if previous_generated is not None:
        if (not previous_generated.get('verified') or previous_generated.get('preserved_existing')
                or not previous_generated.get('version', '').startswith('printed-contents-v')):
            raise ValueError('Only verified generated bookmarks can be replaced')
        check_generated(existing, previous_generated['entries'])
        doc.set_toc([])
        existing = []
    report.update(inserted=0, preserved_existing=len(existing), verified=False)
    if existing:
        report['mode'] = 'preserved_existing'
        report['entries'] = []
        report['review'] = []
        return report
    toc = []
    for entry in report['entries']:
        if not 1 <= entry['page'] <= len(doc):
            raise ValueError('Bookmark destination outside PDF')
        page = doc[entry['page']-1]
        point = fitz.Point(entry['x']*page.rect.width, entry['y']*page.rect.height)
        entry['destination'] = [point.x, point.y]
        toc.append([entry['level'], entry['title'], entry['page'],
                    {'kind': fitz.LINK_GOTO, 'to': point, 'zoom': 0}])
    if toc:
        doc.set_toc(toc)
        # PyMuPDF 1.28's set_toc coordinate conversion loses crop offsets and
        # mishandles 270-degree rotation. Write only our newly created GoTo
        # destinations in PDF user space; do not change any page geometry.
        for row, entry in zip(doc.get_toc(simple=False), report['entries']):
            page = doc[entry['page']-1]
            # Page.rect includes /UserUnit, whereas cropbox and rotation_matrix
            # use unscaled PDF units. Normalize before undoing rotation.
            rotated_width = page.cropbox.height if page.rotation in (90, 270) else page.cropbox.width
            scale = page.rect.width / rotated_width
            point = (fitz.Point(entry['destination']) / scale) * page.derotation_matrix
            x = point.x + page.cropbox.x0
            y = page.mediabox.y1 - page.cropbox.y0 - point.y
            doc.xref_set_key(row[3]['xref'], 'A/D',
                             f'[{page.xref} 0 R /XYZ {x:.6f} {y:.6f} 0]')
        report['inserted'] = len(toc)
    return report


def check_generated(actual, expected):
    if len(actual) != len(expected):
        raise RuntimeError('Bookmark count mismatch; existing bookmarks may have been edited')
    for row, entry in zip(actual, expected):
        dest = row[3]
        if row[:3] != [entry['level'], entry['title'], entry['page']] or dest.get('kind') != fitz.LINK_GOTO:
            raise RuntimeError('Bookmark title, hierarchy or page mismatch')
        if dest.get('page') != entry['page']-1 or 'to' not in dest or abs(dest['to']-fitz.Point(entry['destination'])) > .1:
            raise RuntimeError('Bookmark position mismatch')


def verify_bookmarks(source, output, report):
    """Reopen before publication and reject lost titles, hierarchy or destinations."""
    with fitz.open(source) as original, fitz.open(output) as result:
        if len(original) != len(result):
            raise RuntimeError('Bookmark PDF page count mismatch')
        actual = result.get_toc(simple=False)
        if report['preserved_existing']:
            def semantic(toc):
                return [[*row[:3], {k: v for k, v in row[3].items() if k != 'xref'}] for row in toc]
            if semantic(actual) != semantic(original.get_toc(simple=False)):
                raise RuntimeError('Existing bookmarks changed')
        else:
            check_generated(actual, report['entries'])
    report['verified'] = True
