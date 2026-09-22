"""Read-only PDF batching, scan normalization, and content-bound source identity."""
import hashlib
import io
from pathlib import Path
import pymupdf as fitz
from pypdf import PdfReader, PdfWriter
from ocr_api import API, API_OPTIONS


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
