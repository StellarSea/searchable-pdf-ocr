"""Font fallback and invisible text placement. Preserve source pixels and complete character coverage."""
import os
import re
from pathlib import Path
import pymupdf as fitz
from ocr_render import page_coordinate_scale


FONT_CANDIDATES = [
    ("malgun", r"C:\Windows\Fonts\malgun.ttf"),  # 폭이 좁아 원본에 가깝다
    ("korea", None),  # PyMuPDF 내장 CJK
    ("japan", None),
    ("gulim", r"C:\Windows\Fonts\gulim.ttc"),
]


def _utf16_cmap(cmap):
    """Repair scalar (5/6 hex digit) destinations in generated ToUnicode maps.

    Some MuPDF font embeddings emit <1f3e0> instead of UTF-16BE <d83cdfe0>.
    Explicit arrays also handle ranges crossing a surrogate-pair boundary.
    Already valid UTF-16 destinations and all BMP mappings remain byte-exact.
    """
    token = rb'<([0-9a-fA-F]+)>'

    def section(match):
        kind, body = match.group(2), match.group(3)
        pattern = (token + rb'\s+' + token + rb'\s+<([0-9a-fA-F]{5,6})>'
                   if kind == b'bfrange' else token + rb'\s+<([0-9a-fA-F]{5,6})>')

        def mapping(row):
            start = int(row.group(1), 16)
            end = int(row.group(2), 16) if kind == b'bfrange' else start
            scalar = int(row.group(3 if kind == b'bfrange' else 2), 16)
            if not (0 <= start <= end <= 0xffff and 0x10000 <= scalar
                    and scalar + end - start <= 0x10ffff):
                raise ValueError('Invalid generated supplementary Unicode CMap range')
            values = [b'<' + chr(scalar + i).encode('utf-16-be').hex().encode('ascii') + b'>'
                      for i in range(end - start + 1)]
            if kind == b'bfrange':
                return b'<' + row.group(1) + b'> <' + row.group(2) + b'> [' + b' '.join(values) + b']'
            return b'<' + row.group(1) + b'> ' + values[0]

        # Restrict to complete scalar rows, never array members or code spaces.
        fixed = re.sub(rb'(?m)^[ \t]*' + pattern + rb'[ \t]*$', mapping, body)
        return match.group(1) + fixed + match.group(4)

    return re.sub(rb'(\d+ begin(bfrange|bfchar)\s*\n)(.*?)(end(?:bfrange|bfchar))',
                  section, cmap, flags=re.S)


def repair_generated_font_unicode(doc, first_new_xref):
    """Finalize only newly embedded font maps on the PDF owner, before saving.

    Never rewrite a source font or an original shared ToUnicode stream. Rendering
    glyph IDs and font programs stay unchanged; only extraction mappings change.
    """
    repaired = 0
    seen = set()
    for xref in range(first_new_xref, doc.xref_length()):
        if doc.xref_get_key(xref, 'Type') != ('name', '/Font'):
            continue
        kind, reference = doc.xref_get_key(xref, 'ToUnicode')
        if kind != 'xref':
            continue
        target = int(reference.split()[0])
        if target < first_new_xref or target in seen:
            continue
        seen.add(target)
        before = doc.xref_stream(target)
        if before is None:
            continue
        after = _utf16_cmap(before)
        if after != before:
            doc.update_stream(target, after)
            repaired += 1
    return repaired

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

def insert_invisible_line(page, text, rect, fontname, fontobj, fallback_fonts, *, fit_height=False):
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
    fs = min(rect.width / max(width, 0.01), rect.height * 1.35,
             60.0 * page_coordinate_scale(page))
    if fit_height:
        ascender = max(fo.ascender for (_, fo, _), _ in runs)
        descender = min(fo.descender for (_, fo, _), _ in runs)
        fs = min(fs, rect.height / max(ascender - descender, 0.01))
    # detect_lines trims the box to the ink, so the text has to span the whole
    # box for every glyph to have a character under it. Where the ink is short
    # and wide the height cap cut the size down and left the rest of the box
    # empty -- a table cell reading "10001" carried text across half the cell,
    # and a drag over the other half selected nothing, because a viewer finds
    # no character there. Stretching sideways fills the box without growing the
    # glyphs, which would otherwise reach into the lines above and below.
    if not fit_height and rect.width > width * fs * 1.001 and not text[-1:].isspace():
        # Table cell boxes butt against each other, so a run stretched over the
        # whole box touches its neighbour and the two cells come back out of the
        # PDF as one word. A trailing space rides along in the stretch and keeps
        # them apart; it also gives the cell's padding a character of its own,
        # so a drag crossing it stays selected.
        runs[-1][1] += ' '
        width = sum(fo.text_length(s, fontsize=1) for (_, fo, _), s in runs)
    stretch = 1.0 if fit_height else rect.width / max(width * fs, 0.01)
    linear = lambda m: fitz.Matrix(m.a, m.b, m.c, m.d, 0, 0)
    wider = (linear(page.rotation_matrix) * fitz.Matrix(stretch, 1)
             * linear(page.derotation_matrix))
    x, y = rect.x0, rect.y1 - rect.height * 0.18
    if fit_height:
        y = rect.y0 + ascender * fs
    for (name, fo, path), s in runs:
        if name != fontname:
            page.insert_font(fontname=name, fontfile=path)
        point = fitz.Point(x, y) * page.derotation_matrix
        page.insert_text(point, s, fontname=name, fontsize=fs, render_mode=3,
                         rotate=page.rotation,
                         morph=(point, wider) if stretch > 1.001 else None)
        x += fo.text_length(s, fontsize=fs) * stretch


def insert_invisible_paragraph(page, text, rect, fontname, fontobj, fallback_fonts):
    """Keep ordinary textbox placement; fit overflow with glyph-aware insertion.

    A failed textbox inserts nothing. The old last resort wrote an unbounded
    fixed-size line, losing characters outside the page. A primary font without
    a glyph can also report success while encoding NUL. Route both cases through
    the same bounded, fallback-font insertion used by detected lines.
    Return whether compact placement was needed, so callers retain a review flag.
    """
    if all(char.isspace() or fontobj.has_glyph(ord(char)) for char in set(text)):
        scale = page_coordinate_scale(page)
        fs = (rect.get_area() / (0.68 * max(1, len(text)))) ** 0.5
        fs = max(2.0 * scale, min(fs, rect.height, 40.0 * scale))
        for _ in range(12):
            rc = page.insert_textbox(
                rect * page.derotation_matrix, text, fontname=fontname, fontsize=fs,
                render_mode=3, align=0, rotate=page.rotation)
            if rc >= 0:
                return False
            fs *= 0.8
            if fs < 1.5:
                break
    insert_invisible_line(page, ' '.join(text.split()), rect, fontname, fontobj, fallback_fonts,
                          fit_height=True)
    return True
