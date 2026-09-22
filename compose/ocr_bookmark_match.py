"""Match printed contents to source-bound headings and independently read folios."""
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import re

from ocr_bookmark_layout import rows, extract_contents, body_candidates, identity, key

VERSION = 'printed-contents-v3'


def lexical_key(text):
    return key(text).replace('：', ':').replace('–', '-').replace('—', '-')


def similarity(a, b):
    a, b = lexical_key(a), lexical_key(b)
    if a == b:
        return 1.0
    # A very common glyph confusion in z-transform headings. This affects only
    # comparison; the displayed title and all source text remain untouched.
    fix_z = lambda value: re.sub(r'2-(?=변환|역변환|영역)', 'z-', value)
    a, b = fix_z(a), fix_z(b)
    if re.findall(r'\d+', a) != re.findall(r'\d+', b):
        return 0.0
    symbols = lambda value: ''.join(c for c in value if not c.isalnum())
    if symbols(a) != symbols(b):
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def match_score(entry, candidate, page_supported):
    if candidate['opening'] and entry['kind'] != 'chapter':
        return 0, None
    exact = lexical_key(entry['title']) == lexical_key(candidate['text'])
    if exact:
        return 100+int(candidate['heading'])*10+int(candidate['opening'] and entry['kind'] == 'chapter')*15, 'exact_title'
    ek, ei, et = entry['kind'], entry['ident'], entry['lexical']
    ck, ci, ct = candidate['kind'], candidate['ident'], candidate['lexical']
    if ek == 'chapter':
        if ck == 'chapter' and ei != ci:
            return 0, None
        if ck not in ('chapter', 'plain') or not candidate['heading']:
            return 0, None
        # Some chapter pages prefix the title with a bare chapter number.
        ct = re.sub(r'^0*'+re.escape(ei)+r'\s+', '', ct)
    elif ek in ('section', 'lab'):
        if ck == ek and ci != ei:
            # OCR can fuse "8.5 z-..." into "8.52-...". Accept the
            # expected prefix only at the independently located printed page.
            if not (page_supported and candidate['text'].startswith(ei) and
                    re.match(r'^[2zZ]-', candidate['text'][len(ei):].strip())):
                return 0, None
            ct = candidate['text'][len(ei):].strip()
        elif ck not in (ek, 'plain'):
            return 0, None
        if ck == 'plain' and not (page_supported and candidate['heading']):
            return 0, None
        # A lost decorative section number may leave just its final digit.
        if page_supported and candidate['heading']:
            ct = re.sub(r'^'+re.escape(ei.split('.')[-1])+r'\s+', '', ct)
    elif ck != 'plain':
        return 0, None
    equal = lexical_key(et) == lexical_key(ct)
    if equal:
        if ek == 'chapter':
            return 95+int(candidate['opening'])*30, 'chapter_title'
        return 90+int(candidate['joined'] if 'joined' in candidate else False)*5, 'title_and_printed_page' if page_supported else 'title'
    if page_supported and candidate['heading']:
        ratio = similarity(et, ct)
        # Fuzzy matching is never a book-wide search. Page identity plus a
        # unique heading and a large score margin are required below.
        threshold = .86 if min(len(key(et)), len(key(ct))) >= 8 else .75
        if ratio == 1.0:
            return 88, 'similar_title_and_printed_page'
        if min(len(key(et)), len(key(ct))) >= 4 and ratio >= threshold:
            return 65+ratio*15, 'similar_title_and_printed_page'
    return 0, None


def choose(entry, candidates, target_pages=None, lower=None, upper=None):
    scored = []
    for c in candidates:
        if target_pages is not None and c['page'] not in target_pages:
            continue
        if lower is not None and c['page'] < lower or upper is not None and c['page'] >= upper:
            continue
        score, evidence = match_score(entry, c, target_pages is not None)
        if entry.get('reviewed_from'):
            original = dict(entry, title=entry['reviewed_from'], lexical=identity(entry['reviewed_from'])[2])
            old_score, _ = match_score(original, c, target_pages is not None)
            if old_score > score:
                score, evidence = old_score, 'reviewed_display_title'
        if score:
            scored.append((score, c, evidence))
    # A joined heading and its lexical-only variant at the same visual
    # position are the same destination. Keep the most informative match.
    scored.sort(key=lambda item: -item[0])
    unique = []
    for item in scored:
        c = item[1]
        if not any(c['page'] == other[1]['page'] and abs(c['y']-other[1]['y']) < .012 for other in unique):
            unique.append(item)
    if not unique:
        return None, 'title_not_found', []
    if len(unique) > 1 and unique[0][0]-unique[1][0] < 8:
        return None, 'ambiguous_title', sorted({s[1]['page'] for s in unique})
    score, c, evidence = unique[0]
    return {**c, 'evidence': evidence, 'score': round(score, 3)}, None, [c['page']]


def build_plan(pages, corrections=None):
    all_rows = [rows(page) for page in pages]
    toc_pages, entries = extract_contents(all_rows)
    applied = []
    reviewed_destinations = {}
    for correction in corrections or []:
        matches = [e for e in entries if e['toc_page'] == correction['toc_page']
                   and e['title'] == correction['original_title']
                   and ('chapter' not in correction or e.get('chapter') == correction['chapter'])]
        if len(matches) != 1:
            raise ValueError('Bookmark review must match exactly one original contents entry')
        entry = matches[0]
        if not correction.get('reason'):
            raise ValueError('Bookmark review requires source inspection evidence')
        if 'destination_page' in correction or 'destination_text' in correction:
            page = correction.get('destination_page')
            text = correction.get('destination_text')
            if (type(page) is not int or not 1 <= page <= len(pages)
                    or not isinstance(text, str) or not text.strip()):
                raise ValueError('Reviewed bookmark destination requires a valid page and exact OCR heading')
            reviewed_destinations[id(entry)] = (page, text)
        if 'title' in correction:
            if not isinstance(correction['title'], str) or not correction['title'].strip():
                raise ValueError('Invalid reviewed bookmark title')
            entry['title'] = correction['title']
            entry['reviewed_from'] = correction['original_title']
            kind, ident, lexical = identity(entry['title'])
            entry.update(kind=kind, ident=ident, lexical=lexical)
        if 'printed_page' in correction:
            from ocr_bookmark_layout import number
            folio = number(str(correction['printed_page']))
            if not folio:
                raise ValueError('Invalid reviewed printed page')
            entry['printed_page'], entry['folio'] = str(correction['printed_page']), list(folio)
        applied.append(dict(correction))
    candidates, folios, opening = body_candidates(all_rows, toc_pages)
    report = {'version': VERSION, 'mode': 'printed_contents' if toc_pages else 'headings',
              'toc_pages': sorted(i+1 for i in toc_pages), 'entries': [], 'review': [],
              'detected_entries': len(entries)}
    if applied:
        report['applied_review'] = applied
    by_folio = defaultdict(set)
    for page, values in folios.items():
        if len(values) == 1:
            by_folio[next(iter(values))].add(page)
    if not toc_pages:
        # Without printed contents, restrict fallback to actual numbered
        # headings outside chapter introductions; never mine numbered exercises.
        for c in candidates:
            if c['heading'] and not c['opening'] and c['kind'] in ('chapter', 'section') and c['lexical']:
                if re.search(r'(?:다음|빈칸|구하[라시오]|설명하|증명하|옳으면)', c['lexical']):
                    continue
                entries.append({**c, 'title': c['text'], 'printed_page': None, 'folio': None, 'toc_page': None,
                                'chapter': c['ident'].split('.')[0], 'rank': 1 if c['kind'] == 'chapter' else len(c['ident'].split('.'))})
        entries.sort(key=lambda e: (e['page'], e['y'], e['x']))
        report['detected_entries'] = len(entries)
    chosen = [None]*len(entries)
    failures = [('title_not_found', []) for _ in entries]
    target_pages = [None]*len(entries)
    for i, e in enumerate(entries):
        if e['folio']:
            found = by_folio.get(tuple(e['folio']), set())
            if found:
                target_pages[i] = found
            else:
                # A gap in printed numbering between adjacent source pages is
                # evidence of a missing leaf, not permission to guess a target.
                style, value = e['folio']
                below = [(v, ps) for (s, v), ps in by_folio.items() if s == style and v < value]
                above = [(v, ps) for (s, v), ps in by_folio.items() if s == style and v > value]
                if below and above:
                    low, high = max(below), min(above)
                    if len(low[1]) == len(high[1]) == 1 and next(iter(high[1])) == next(iter(low[1]))+1:
                        target_pages[i] = set()
        eligible = candidates
        if e['folio']:
            eligible = [c for c in candidates if not folios.get(c['page']) or tuple(e['folio']) in folios[c['page']]]
        chosen[i], reason, hits = choose(e, eligible, target_pages[i])
        failures[i] = reason, hits
        if target_pages[i] == set():
            failures[i] = 'source_page_missing', []
        if id(e) in reviewed_destinations:
            page, text = reviewed_destinations[id(e)]
            reviewed = [c for c in candidates if c['page'] == page and c['text'] == text and c['heading']]
            if len(reviewed) != 1:
                raise ValueError('Reviewed bookmark destination must match exactly one OCR heading')
            chosen[i] = dict(reviewed[0], evidence='visually_reviewed_destination', score=150)
            target_pages[i] = {page}
            failures[i] = None, [page]
    # Use neighboring exact matches only to bridge missing printed folios.
    # Local agreement avoids carrying one offset through inserted pages.
    anchors = [(i, hit['page']-e['folio'][1], e['folio'][0]) for i, (e, hit) in enumerate(zip(entries, chosen))
               if hit and e['folio'] and hit['evidence'] != 'similar_title_and_printed_page']
    for i, e in enumerate(entries):
        if not e['folio'] or target_pages[i] is not None:
            continue
        before = [a for a in anchors if a[0] < i and a[2] == e['folio'][0]]
        after = [a for a in anchors if a[0] > i and a[2] == e['folio'][0]]
        if before and after and before[-1][1] == after[0][1]:
            target_pages[i] = {e['folio'][1]+before[-1][1]}
            chosen[i], reason, hits = choose(e, candidates, target_pages[i])
            failures[i] = reason, hits
    chapters = {e['ident']: c['page'] for e, c in zip(entries, chosen) if e['kind'] == 'chapter' and c}
    chapter_starts = sorted(set(chapters.values()))
    for i, e in enumerate(entries):
        if chosen[i] or e['folio'] or e['kind'] == 'chapter':
            continue
        lower = chapters.get(e.get('chapter'))
        upper = next((p for p in chapter_starts if lower and p > lower), None)
        if lower:
            chosen[i], reason, hits = choose(e, candidates, lower=lower, upper=upper)
            failures[i] = reason, hits
    stack, previous, seen = [], (0, 0.0), set()
    for i, (entry, hit) in enumerate(zip(entries, chosen)):
        rank = entry['rank']
        while stack and stack[-1][0] >= rank:
            stack.pop()
        reason, hits = failures[i]
        if hit:
            target = hit['page'], hit['y']
            if (key(entry['title']), hit['page']) in seen:
                reason = 'duplicate_entry'
            elif target < previous:
                reason = 'destination_out_of_order'
        item = {'title': entry['title'], 'toc_page': entry['toc_page'], 'printed_page': entry['printed_page'],
                'kind': entry['kind'], 'ident': entry['ident'], 'chapter': entry.get('chapter'),
                'toc_index': i, 'raw_rank': rank}
        accepted = hit is not None and reason is None
        if accepted:
            item.update(level=1+sum(ok for _, ok in stack), page=hit['page'], x=hit['x'], y=hit['y'],
                        evidence=hit['evidence'], matched_text=hit['text'], score=hit['score'])
            report['entries'].append(item)
            previous = target
            seen.add((key(entry['title']), hit['page']))
        else:
            item.update(reason=reason or 'title_not_found', candidate_pages=hits)
            report['review'].append(item)
        stack.append((rank, accepted))
    report['evidence_counts'] = dict(Counter(e['evidence'] for e in report['entries']))
    return report
