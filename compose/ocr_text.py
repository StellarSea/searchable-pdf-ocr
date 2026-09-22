"""Text normalization, content-preservation guards, and line alignment. No network or PDF writes."""
import html as _html
import re
from collections import Counter
from difflib import SequenceMatcher
from html.parser import HTMLParser
import numpy as np


HTML_TAG_RE = re.compile(r"</?(?:table|tr|td|th|p|div|br|span|b|strong|i|em|img)(?=[\s/>])", re.I)

class _LiteralHTMLParser(HTMLParser):
    """Retain source spelling of unknown closing tags (e.g. code examples)."""

    def parse_endtag(self, i):
        # HTMLParser's end-tag callback otherwise supplies only a lowercase name.
        end = self.rawdata.find('>', i)
        self.literal_endtag = self.rawdata[i:end+1] if end >= 0 else ''
        return super().parse_endtag(i)

    def handle_startendtag(self, tag, attrs):
        # A literal <Node/> is one token, not an opening and invented closing tag.
        if HTML_TAG_RE.match(self.get_starttag_text()):
            self.literal_endtag = f'</{tag}>'
            super().handle_startendtag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)


class _TextParser(_LiteralHTMLParser):
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
        elif not HTML_TAG_RE.match(self.literal_endtag):
            self.parts.append(self.literal_endtag)

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

HANGUL_RE = re.compile(r'[가-힣]')

CJK_RE = re.compile(r'[一-鿿㐀-䶿぀-ヿ]')

def line_language(text):
    """Pick the recognizer whose dictionary covers more of this block."""
    plain = strip_html(text)
    return 'korean' if len(HANGUL_RE.findall(plain)) > len(CJK_RE.findall(plain)) else None

TOC_KEEP = re.compile(
    r"(Part\s*\d|Unit\s*\d|Chapter\s*\d|Test\s*\d|DAY\s*\d|정답|해설|목차|부록)",
    re.IGNORECASE,
)

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

class _TableParser(_LiteralHTMLParser):
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
        elif self.cell is not None and not HTML_TAG_RE.match(self.get_starttag_text()):
            self.cell.append(_html.escape(self.get_starttag_text(), quote=False))

    def handle_data(self, data):
        if self.cell is not None:
            # Cell strings are passed through strip_html later. Escape decoded
            # entities so literal <b> and &lt; are not interpreted a second time.
            self.cell.append(_html.escape(data, quote=False))

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None and self.row is not None:
            self.row.append((''.join(self.cell).strip(), self.span))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif self.cell is not None and not HTML_TAG_RE.match(self.literal_endtag):
            self.cell.append(_html.escape(self.literal_endtag, quote=False))

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

def repair_is_safe(old, new):
    old, new = strip_html(old), strip_html(new)
    if len(new) <= max(len(old) * 1.3, 20):
        return False
    tokens = lambda s: Counter(re.findall(r"\w+", s.casefold()))
    before, after = tokens(old), tokens(new)
    coverage = sum((before & after).values()) / max(1, sum(before.values()))
    critical = lambda s: set(re.findall(r"[\w.+-]+@[\w.-]+|\d+(?:[.,-]\d+)*", s.casefold()))
    return (not before or coverage >= 0.95) and critical(old) <= critical(new)

def _similarity_below(before, after, threshold):
    """Exact SequenceMatcher decision, with cheap equality/upper-bound exits."""
    if before == after:
        return 1.0 < threshold
    matcher = SequenceMatcher(None, before, after, autojunk=False)
    return (matcher.real_quick_ratio() < threshold
            or matcher.quick_ratio() < threshold
            or matcher.ratio() < threshold)


def _critical_tokens(text):
    """Keep critical tokens in reading order, including repeated occurrences."""
    # Most OCR blocks contain no address. Trying an unbounded email prefix at
    # every character of a dense paragraph otherwise causes quadratic scanning.
    if '@' not in text:
        return re.findall(r'\d+(?:[.,-]\d+)*', text)
    return re.findall(r'[\w.+-]+@[\w.-]+|\d+(?:[.,-]\d+)*', text)


def line_refinement_is_safe(original, candidates):
    """Require near-identical content and preserve critical tokens, not just length."""
    if not candidates or any(not c.strip() for c in candidates):
        return False
    normalize = lambda s: ''.join(strip_html(s).split())
    before, after = normalize(original), normalize(''.join(candidates))
    if not before or _critical_tokens(before) != _critical_tokens(after):
        return False
    return not _similarity_below(before, after, 0.94)

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
    if _critical_tokens(before) != _critical_tokens(after):
        return True
    return _similarity_below(before, after, threshold)

def line_boundaries_are_usable(original, candidates):
    """Accept approximate OCR only as a boundary guide; its characters are never copied."""
    if not candidates or any(not candidate.strip() for candidate in candidates):
        return False
    normalize = lambda value: ''.join(strip_html(value).split()).casefold()
    before, after = normalize(original), normalize(''.join(candidates))
    if not before or not after:
        return False
    length_ratio = len(after) / len(before)
    return 0.75 <= length_ratio <= 1.25 and not _similarity_below(before, after, 0.88)

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
    operations = ([('equal', 0, len(before), 0, len(after))] if before == after else
                  SequenceMatcher(None, before, after, autojunk=False).get_opcodes())
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
