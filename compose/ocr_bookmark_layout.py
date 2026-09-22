"""Read-only interpretation of OCR layout for printed contents and headings."""
import math
import re
import unicodedata

from ocr_schema import _find_blocks, _find_size
from ocr_text import strip_html, _TableParser

HEADINGS = {'doc_title', 'paragraph_title', 'section_title', 'title', 'heading'}
EXCLUDED = {'header', 'footer', 'header_image', 'footer_image', 'page_number', 'number'}
NUMBER = r'(?:[0-9]{1,5}|[ivxlcdmIVXLCDM]{1,12})'
ENTRY = re.compile(r'^(.+?)(?:\s*[.·…‥_]{2,}\s*|\s+)(' + NUMBER + r')\s*$')
CHAPTER = re.compile(r'^(?:chapter|[A-Z]{2,7})\s*0*(\d+)\s+(.+)$', re.I)
SECTION = re.compile(r'^((?:\d+|[A-Z])(?:\.\d+)+)\s*(.*)$')
LAB = re.compile(r'^lab\s*(\d+(?:\.\d+)+)\s*(.*)$', re.I)


def clean(text):
    text = re.sub(r'^#{1,6}\s+', '', strip_html(text).strip()).strip(' *`')
    text = re.sub(r'^[▪■●•◆▶☐☑✓✔]\s*', '', text).strip()
    decorative = re.fullmatch(r'\|?\s*(연습문제|학습목표|단원개요)\s*\|?', text)
    if decorative:
        text = decorative[1]
    return re.sub(r'\s+\|$', '', text).strip()


def key(text):
    text = clean(text)
    if re.fullmatch(r'[가-힣](?:\s*/\s*[가-힣])+', text):
        text = text.replace('/', '')
    return ''.join(c for c in unicodedata.normalize('NFC', text).casefold() if not c.isspace())


def marker(text):
    return key(text) in {'목차', '차례', 'contents', 'tableofcontents', 'contents(continued)', 'contentscontinued'}


def number(token):
    if re.fullmatch(r'\d{1,5}', token):
        return ('arabic', int(token)) if int(token) > 0 else None
    token = token.upper()
    if not token or not re.fullmatch(r'M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})', token):
        return None
    digits = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
    value = sum(-digits[c] if i+1 < len(token) and digits[c] < digits[token[i+1]] else digits[c]
                for i, c in enumerate(token))
    return 'roman', value


def identity(title):
    """Numbering identity is separate from the lexical title; never alter OCR."""
    title = clean(title)
    appendix = re.match(r'^(?:부록|appendix)\s*([A-Z])\s+(.+)$', title, re.I)
    if appendix:
        return 'chapter', appendix[1].upper(), appendix[2]
    m = CHAPTER.match(title)
    if m:
        return 'chapter', str(int(m[1])), m[2]
    m = re.match(r'^제?\s*(\d+)\s*장\s*(.*)', title)
    if m:
        return 'chapter', str(int(m[1])), m[2]
    m = LAB.match(title)
    if m:
        return 'lab', m[1], m[2]
    m = SECTION.match(title)
    if m:
        return 'section', m[1], m[2]
    return 'plain', None, title


def rows(page):
    size = _find_size(page)
    if not size or not all(math.isfinite(v) and v > 0 for v in size):
        return []
    result = []
    blocks = page.get('parsing_res_list', [page])
    for bi, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        label = block.get('block_label', '')
        for text, box in _find_blocks(block):
            if not all(math.isfinite(v) for v in box) or not (0 <= box[0] < box[2] <= size[0] and 0 <= box[1] < box[3] <= size[1]):
                continue
            table = '<table' in text.lower()
            if table:
                parser = _TableParser()
                parser.feed(text)
                if not parser.supported:
                    continue
                lines = [' '.join(strip_html(cell) for cell, _ in row) for row in parser.rows]
            else:
                lines = text.splitlines()
            for li, line in enumerate(lines):
                if not clean(line):
                    continue
                result.append({'text': clean(line), 'label': label, 'block': bi, 'line': li,
                    'heading': label in HEADINGS or bool(re.match(r'^\s*#{1,6}\s+', line)),
                    'single': len(lines) == 1, 'table': table,
                    'x': box[0]/size[0], 'right': box[2]/size[0],
                    'y': (box[1]+(box[3]-box[1])*li/max(1, len(lines)))/size[1],
                    'height': (box[3]-box[1])/max(1, len(lines))/size[1]})
    return result


def printed_entries(page_rows):
    """Retain unnumbered chapter headings and missing-folio entries explicitly."""
    usable = [dict(r) for r in page_rows if r['label'] not in EXCLUDED and not marker(r['text'])]
    used = set()
    result = []
    for i, row in enumerate(usable):
        if i in used or number(row['text']):
            continue
        title, folio = row['text'], None
        match = ENTRY.match(title)
        # A standalone chapter label is not a title followed by a page
        # number. Treating CHAPTER 01 as page 1 turned chapter introductions
        # containing a small "contents" panel into whole-book contents.
        chapter_label = re.fullmatch(r'chapter\s*\d+', title, re.I)
        if match and number(match[2]) and not chapter_label:
            title, folio = match[1].rstrip(' .·…‥_'), match[2]
        elif (i+1 < len(usable) and number(usable[i+1]['text'])
              and usable[i+1]['block'] == row['block']
              and not (i+2 < len(usable) and number(usable[i+2]['text'])
                       and usable[i+2]['block'] == row['block'])):
            folio = usable[i+1]['text']
            used.add(i+1)
        else:
            # Numeric column stored as a separate block: require a unique
            # nearest folio on the same visual row, never jump over a title.
            nearby = [(j, other) for j, other in enumerate(usable) if j not in used and number(other['text'])
                      and other['x'] >= row['right']
                      and abs(other['y']+other['height']/2-row['y']-row['height']/2)
                          <= min(row['height'], other['height'])*0.4]
            if len(nearby) == 1:
                j, other = nearby[0]
                if not any(row['right'] < m['x'] < other['x'] and abs(m['y']-row['y']) < row['height']/2 for m in usable):
                    folio = other['text']
                    used.add(j)
        if not 2 <= len(key(title)) <= 180:
            continue
        result.append({**row, 'title': title, 'printed_page': folio,
                       'folio': list(number(folio)) if folio else None})
    # A lowercase continuation can wrap a title. Never combine two numbered
    # items or a chapter heading and its first section.
    joined = []
    for row in result:
        if (joined and joined[-1]['folio'] is None and identity(joined[-1]['title'])[0] == 'plain'
                and re.match(r'^[a-z]', row['title']) and row['folio']
                and abs(row['x']-joined[-1]['x']) < .04
                and 0 <= row['y']-joined[-1]['y'] <= joined[-1]['height']*2.5):
            prior = joined.pop()
            row = {**row, 'title': prior['title']+' '+row['title'], 'x': prior['x'], 'y': prior['y']}
        joined.append(row)
    return joined


def extract_contents(all_rows):
    toc_pages, entries = set(), []
    for index, page_rows in enumerate(all_rows):
        parsed = printed_entries(page_rows)
        # Repairs may add a standalone chapter heading ahead of the original
        # multi-line contents block. Keep the chapter in that block's reading
        # order, not before entries that actually precede it on the page.
        inline_chapters = {identity(e['title'])[:2] for e in parsed
                           if not e['single'] and identity(e['title'])[0] == 'chapter'}
        parsed = [e for e in parsed if not (e['single'] and e['heading']
                  and identity(e['title'])[:2] in inline_chapters)]
        numbered = sum(e['folio'] is not None for e in parsed)
        marker_found = any(marker(row['text']) for row in page_rows)
        leaders = sum(bool(re.search(r'[.·…‥_]{2,}', row['text'])) for row in parsed)
        dense = numbered >= 4 and numbered >= len(parsed)*.65 and leaders >= 2
        continuing = index-1 in toc_pages and numbered >= 2 and numbered >= len(parsed)*.5
        # A continuation can lose its entire folio column in OCR. A repeated
        # contents label and several numbered sections still identify it;
        # dropping this page silently drops complete chapters.
        labelled_continuation = (any(marker(r['text']) and r['y'] < .2 for r in page_rows)
                                 and index-1 in toc_pages
                                 and sum(identity(e['title'])[0] == 'section' for e in parsed) >= 2)
        # A damaged middle contents page can lose every folio and its marker.
        # Require a dense contents-labelled block AND a following page whose
        # independent title/folio pairs establish continuation on both sides.
        next_entries = printed_entries(all_rows[index+1]) if index+1 < len(all_rows) else []
        bridged_continuation = (index-1 in toc_pages and len(parsed) >= 8
                                and sum(e['label'] == 'content' for e in parsed) >= len(parsed)*.7
                                and sum(identity(e['title'])[0] == 'section' for e in parsed) >= 2
                                and sum(e['folio'] is not None for e in next_entries) >= max(4,len(next_entries)*.65))
        if ((marker_found and numbered >= 1) or dense or continuing
                or labelled_continuation or bridged_continuation):
            toc_pages.add(index)
            entries.extend({**e, 'toc_page': index+1} for e in parsed)
    # Infer a chapter's omitted OCR prefix only from the following X.1 entry.
    # Explicit duplicate headings can arise when a repaired content block
    # overlaps its original heading block; retain the first semantic chapter.
    chapter, section, seen_chapters, output = None, None, set(), []
    for i, entry in enumerate(entries):
        kind, ident, lexical = identity(entry['title'])
        if kind == 'plain' and not entry['folio'] and i+1 < len(entries):
            nk, ni, _ = identity(entries[i+1]['title'])
            if nk == 'section' and ni.endswith('.1') and ni.count('.') == 1 and ni.split('.')[0] != chapter:
                kind, ident = 'chapter', ni.split('.')[0]
                # Some blocks contain "10 탐색" rather than "CHAPTER 10 탐색".
                lexical = re.sub(r'^0*'+re.escape(ident)+r'\s+', '', lexical)
        if kind == 'chapter':
            if ident in seen_chapters and entry['folio'] is None:
                continue
            seen_chapters.add(ident)
            chapter, section = ident, None
        elif kind == 'section':
            section = ident
            chapter = chapter or ident.split('.')[0]
        rank = (1 if kind == 'chapter' or key(lexical) in {'찾아보기', '착아보기'} else len(ident.split('.'))+int(bool(seen_chapters)) if kind == 'section'
                else 2 if key(lexical) == '연습문제' else
                (len(section.split('.'))+1+int(bool(seen_chapters))) if section else 1)
        output.append({**entry, 'kind': kind, 'ident': ident, 'lexical': lexical,
                       'chapter': chapter, 'section': section, 'rank': rank})
    return toc_pages, output


def body_candidates(all_rows, toc_pages):
    candidates, folios, opening = [], {}, set()
    for index, page_rows in enumerate(all_rows):
        if index in toc_pages:
            continue
        numbers = {number(r['text'].strip(' -–—')) for r in page_rows
                   if r['label'] not in HEADINGS | {'header_image', 'footer_image'}
                   and (r['y'] > .85 or r['y'] < .12)} - {None}
        folios[index+1] = numbers
        has_chapter = any(re.fullmatch(r'(?:chapter(?:\s*\d+)?|appendix(?:\s*[A-Z])?)', r['text'], re.I)
                          for r in page_rows)
        overview = any(key(r['text']) in {'단원개요', '학습목표'} for r in page_rows)
        # Actual section/subsection headings may share a page with learning
        # objectives. Only a list of sections is evidence of a chapter cover.
        section_count = sum(identity(r['text'])[0] == 'section' and not r['heading'] for r in page_rows)
        if ((overview and (has_chapter or section_count >= 2))
                or (has_chapter and section_count >= 2)):
            opening.add(index+1)
        good = []
        for r in page_rows:
            if r['label'] in EXCLUDED or r['table'] or marker(r['text']) or number(r['text']):
                continue
            if not (r['single'] or r['heading']) or not (1 if r['heading'] else 2) <= len(key(r['text'])) <= 180:
                continue
            good.append({**r, 'page': index+1, 'opening': index+1 in opening})
        # Rejoin heading numbers and adjacent title blocks. Geometry prevents
        # concatenation of two independent headings or a running header.
        additions = []
        for r in good:
            if not r['heading'] or not re.fullmatch(r'\d+(?:\.\d+)+', r['text']):
                continue
            near = [s for s in good if s is not r and s['heading'] and identity(s['text'])[0] == 'plain'
                    and ((s['x'] >= r['right'] and abs(s['y']-r['y']) <= max(s['height'], r['height'])*.6)
                         or (abs(s['x']-r['x']) < .04 and 0 < s['y']-r['y'] <= r['height']*2))]
            if len(near) == 1:
                s = near[0]
                additions.append({**s, 'text': r['text']+' '+s['text'], 'x': r['x'], 'y': min(r['y'], s['y']), 'joined': True})
        for r in good+additions:
            kind, ident, lexical = identity(r['text'])
            candidates.append({**r, 'kind': kind, 'ident': ident, 'lexical': lexical})
    marginal = {}
    for c in candidates:
        if c['y'] < .1 or c['y'] > .9:
            marginal.setdefault(key(c['text']), set()).add(c['page'])
    candidates = [c for c in candidates if not ((c['y'] < .1 or c['y'] > .9)
                  and len(marginal.get(key(c['text']), set())) >= 3)]
    return candidates, folios, opening
