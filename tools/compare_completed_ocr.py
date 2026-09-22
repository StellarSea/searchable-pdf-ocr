"""Read-only comparison of two completed OCR runs, including exact PDF findings."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
from ocr_storage import atomic_json


def read_report(folder, stem):
    path = folder/'.ocr'/f'{stem}_line_ocr.sqlite3'
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        row = db.execute('SELECT content FROM artifacts WHERE name=?', (f'{stem}_auto_report.json',)).fetchone()
        if row is None:
            raise ValueError(f'Missing completed report in {path}')
        return json.loads(row[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--stem', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError('Use a new comparison result path')
    baseline, candidate = [read_report(folder, args.stem) for folder in (args.baseline, args.candidate)]
    if baseline['source_identity']['sha256'] != candidate['source_identity']['sha256']:
        raise ValueError('Reports belong to different source documents')
    differences = {'pixels': [], 'text_coordinates': [], 'page_findings': []}
    with fitz.open(args.baseline/f'{args.stem}_auto_searchable.pdf') as old, \
         fitz.open(args.candidate/f'{args.stem}_auto_searchable.pdf') as new:
        if not len(old) == len(new) == len(baseline['pages']) == len(candidate['pages']):
            raise ValueError('PDF/report page count mismatch')
        for index in range(len(old)):
            a, b = old[index], new[index]
            if a.get_text('rawdict') != b.get_text('rawdict'):
                differences['text_coordinates'].append(index+1)
            ap, bp = a.get_pixmap(), b.get_pixmap()
            if ap.irect != bp.irect or ap.samples != bp.samples:
                differences['pixels'].append(index+1)
            if baseline['pages'][index] != candidate['pages'][index]:
                differences['page_findings'].append(index+1)
    line_decisions_equal = baseline.get('line_decisions') == candidate.get('line_decisions')
    result = {'differences': differences, 'line_decisions_equal': line_decisions_equal,
              'exact_pixels_text_coordinates_and_findings': not any(differences.values()) and line_decisions_equal,
              'baseline_elapsed_seconds': baseline['elapsed_seconds'],
              'candidate_elapsed_seconds': candidate['elapsed_seconds'],
              'candidate_status': candidate['status'],
              'validation_failed': candidate['validation_failed'],
              'cpu': candidate.get('cpu_prepare_stats'), 'gpu': candidate.get('gpu_feed_stats'),
              'verification': candidate.get('verification_stats')}
    atomic_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if not result['exact_pixels_text_coordinates_and_findings'] or result['validation_failed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
