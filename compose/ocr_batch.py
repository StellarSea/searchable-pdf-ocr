"""Run the unattended OCR on every PDF in a folder, one after another.

Each document runs as its own process, so a failure in one cannot take the rest
of the batch down, and the per-document resume already built into the pipeline
still applies: rerunning the batch continues where it stopped. A document whose
recorded status says it finished, with a matching source fingerprint, is skipped
without touching the OCR servers.

Documents are ordered by page count by default so that a configuration problem
shows up on the cheapest document rather than after an hour of work.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from pypdf import PdfReader

import ocr_modes
from ocr_source import source_identity
from ocr_storage import atomic_json
import ocr_workflow as workflow

DONE = {'completed', 'completed_with_warnings'}


def pages_of(path):
    try:
        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0


def status_tag(extra):
    """Name the status file the way the pipeline itself will.

    A --debug-lines run writes its own status, so asking about the normal one
    would skip every document that had already been processed normally.
    """
    return ocr_modes.run_tag(ocr_modes.build_parser().parse_args(['unused.pdf', *extra]))


def finished(src, out_dir, tag='_auto', options=None):
    """True when a previous run of this exact source, in this mode, completed."""
    status_path = workflow.find_artifact(out_dir, f'{src.stem}{tag}_status.json')
    if not status_path.exists():
        return False
    try:
        status = json.loads(status_path.read_text(encoding='utf-8'))
        identity = status.get('source_identity')
    except (ValueError, OSError):
        return False
    return (status.get('status') in DONE
            and bool(status.get('output'))
            and (Path(status.get('output', '')).is_file()
                 or workflow.find_artifact(out_dir, Path(status.get('output', '')).name).is_file())
            and identity == source_identity(src)
            and (options is None or status.get('options') == options))


def run_one(src, out_dir, log_dir, extra, tag='_auto'):
    log = log_dir / f'{src.stem}{tag}.log'
    command = [sys.executable, str(Path(__file__).parent / 'ocr_to_searchable_pdf.py'),
               str(src), *extra, '--out', str(out_dir)]
    started = time.time()
    with log.open('w', encoding='utf-8', errors='replace') as handle:
        code = subprocess.call(command, stdout=handle, stderr=subprocess.STDOUT)
    elapsed = time.time() - started
    status = 'failed'
    review = None
    path = workflow.find_artifact(out_dir, f'{src.stem}{tag}_status.json')
    if code == 0 and path.exists():
        try:
            recorded = json.loads(path.read_text(encoding='utf-8'))
            if recorded.get('status') in DONE:
                status = recorded['status']
                review = len(recorded.get('review_pages') or [])
        except (ValueError, OSError):
            pass
    return {'source': src.name, 'pages': pages_of(src), 'status': status,
            'exit_code': code, 'seconds': round(elapsed, 1),
            'review_pages': review, 'log': str(log)}


def discover_sources(folder, recursive=False, out=None):
    folder = folder.resolve()
    excluded = out.resolve() if out else None
    pattern = '**/*.pdf' if recursive else '*.pdf'
    return [p for p in sorted(folder.glob(pattern)) if p.is_file()
            and not any(part.lower() in {'ocr_output', '.ocr_cache', '.ocr'}
                        for part in p.relative_to(folder).parts[:-1])
            and not (excluded and p.resolve().is_relative_to(excluded))
            and not p.name.lower().endswith(('_searchable.pdf', '_debug.pdf', '.partial.pdf'))]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('folder', type=Path, help='folder holding the scanned PDFs')
    ap.add_argument('--recursive', action='store_true')
    ap.add_argument('--order', choices=['pages', 'name', 'size'], default='pages')
    ap.add_argument('--redo', action='store_true', help='process even already finished documents')
    ap.add_argument('--out', type=Path, help='output folder (default: ocr_output beside each PDF)')
    ap.add_argument('--stop-on-failure', action='store_true')
    # Unrecognized flags are forwarded to ocr_to_searchable_pdf.py. A positional
    # REMAINDER cannot be used here: it captures this script's own options too.
    args, extra = ap.parse_known_args()
    extra = [a for a in extra if a != '--']
    parsed = ocr_modes.build_parser().parse_args(['unused.pdf', *extra])
    options = ocr_modes.run_options(parsed)
    sources = discover_sources(args.folder, args.recursive, args.out)
    if not sources:
        sys.exit(f'No PDF found in {args.folder}')
    key = {'pages': pages_of, 'name': lambda p: p.name.lower(),
           'size': lambda p: p.stat().st_size}[args.order]
    sources.sort(key=key)
    if args.out:
        names = [p.stem.casefold() for p in sources]
        if len(names) != len(set(names)):
            ap.error('Duplicate PDF names would overwrite results in --out; use separate output folders')

    tag = status_tag(extra)
    log_dir = workflow.artifact_path(args.out.resolve() if args.out else args.folder / 'ocr_output', 'batch_logs')
    log_dir.mkdir(parents=True, exist_ok=True)
    results = []
    total = len(sources)
    for index, src in enumerate(sources, 1):
        out_dir = args.out.resolve() if args.out else src.parent / 'ocr_output'
        out_dir.mkdir(parents=True, exist_ok=True)
        head = f'[batch {index}/{total}] {src.name} ({pages_of(src)}p)'
        if (not (args.redo or parsed.no_cache or parsed.repair_cache)
                and finished(src, out_dir, tag, options)):
            print(f'{head} already finished; skipping', flush=True)
            results.append({'source': src.name, 'pages': pages_of(src),
                            'status': 'skipped', 'exit_code': 0, 'seconds': 0.0})
            continue
        print(f'{head} starting', flush=True)
        result = run_one(src, out_dir, log_dir, extra, tag)
        print(f'{head} -> {result["status"]} in {result["seconds"]}s'
              + (f', {result["review_pages"]} pages flagged' if result['review_pages'] is not None else '')
              + (f' (see {result["log"]})' if result['status'] == 'failed' else ''), flush=True)
        results.append(result)
        if args.stop_on_failure and result['status'] == 'failed':
            print('[batch] stopping after a failure as requested', flush=True)
            break

    summary_suffix = '' if tag == '_auto' else '_debug' if tag == '_auto_debug' else tag
    summary = log_dir.parent / f'batch_status{summary_suffix}.json'
    atomic_json(summary, {'finished': time.time(), 'documents': results})
    print('\n[batch] summary')
    for r in results:
        print(f"  {r['status']:26s} {r['pages']:5d}p  {r['seconds']:8.1f}s  {r['source']}")
    failed = [r for r in results if r['status'] == 'failed']
    print(f"[batch] {len(results)-len(failed)}/{len(results)} ok -> {summary}")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
