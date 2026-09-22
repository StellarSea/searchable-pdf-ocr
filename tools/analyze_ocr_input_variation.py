"""Locate upstream text/geometry changes and shared line-cache response changes.

Both completed databases are read-only. Restoring repair_audit.original isolates
the recorded document OCR text, not ground-truth accuracy or causal proof.
"""
import argparse
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3


def read(folder, stem):
    path = folder/'.ocr'/f'{stem}_line_ocr.sqlite3'
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as conn:
        def artifact(suffix):
            row = conn.execute('SELECT content FROM artifacts WHERE name=?', (stem+suffix,)).fetchone()
            if row is None:
                raise ValueError(f'Missing artifact {suffix}')
            return json.loads(row[0])
        report, pages = artifact('_auto_report.json'), artifact('_pruned.json')
        responses = dict(conn.execute('SELECT key, value FROM responses'))
    return report, pages, responses


def upstream(pages):
    result = copy.deepcopy(pages)
    for page in result:
        for block in page.get('parsing_res_list', []):
            audit = block.pop('repair_audit', None)
            block.pop('repair_error', None)
            if audit is not None:
                block['block_content'] = audit['original']
    return result


def compare(before, after):
    if len(before) != len(after):
        raise ValueError('Page count mismatch')
    changed_text, changed_geometry = [], []
    fields = ('block_bbox', 'block_label', 'block_id', 'block_order')
    for index, (old, new) in enumerate(zip(upstream(before), upstream(after)), 1):
        a, b = old.get('parsing_res_list', []), new.get('parsing_res_list', [])
        if [v.get('block_content') for v in a] != [v.get('block_content') for v in b]:
            changed_text.append(index)
        if [[v.get(k) for k in fields] for v in a] != [[v.get(k) for k in fields] for v in b]:
            changed_geometry.append(index)
    return {'upstream_text_changed_pages': changed_text, 'upstream_geometry_changed_pages': changed_geometry}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--stem', required=True)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError('Choose a new diagnostic output')
    old, before, a = read(args.baseline, args.stem)
    new, after, b = read(args.candidate, args.stem)
    if old['source_identity']['sha256'] != new['source_identity']['sha256']:
        raise ValueError('Source identity mismatch')
    shared = a.keys() & b.keys()
    result = {**compare(before, after), 'shared_line_response_keys': len(shared),
              'different_shared_line_response_keys': sorted(k for k in shared if a[k] != b[k]),
              'baseline_only_line_keys': len(a.keys()-b.keys()),
              'candidate_only_line_keys': len(b.keys()-a.keys()),
              'scope': 'Recorded input variation, not accuracy or causal attribution'}
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
