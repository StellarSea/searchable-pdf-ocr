"""Run inside an isolated Compose line-service container; no production cache writes."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image
sys.path.insert(0, '/app')
import line_ocr_server as server


def gpu():
    result = subprocess.run(['nvidia-smi', '--query-gpu=memory.used,memory.free,utilization.gpu',
                             '--format=csv,noheader,nounits'], capture_output=True, text=True, check=True)
    used, free, utilization = map(int, result.stdout.strip().splitlines()[0].split(','))
    return {'used_mib': used, 'free_mib': free, 'utilization': utilization}


async def run(args):
    corpus = json.loads(args.corpus.read_text())
    decoded = {}
    for case in corpus['cases']:
        for name in case['files']:
            if name not in decoded:
                with Image.open(args.corpus.parent/name) as image:
                    decoded[name] = np.asarray(image.convert('RGB'))
    samples, done = [], threading.Event()
    def monitor():
        while not done.is_set():
            try:
                samples.append(gpu())
            except Exception:
                pass
            done.wait(.2)
    telemetry = threading.Thread(target=monitor, daemon=True)
    telemetry.start()
    report = {'workers': server.WORKERS, 'batch_size': server.BATCH_SIZE,
              'corpus_sha256': hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
              'input_width': server.INPUT_WIDTH, 'rounds': args.rounds, 'cases': [],
              'before_init': gpu()}
    try:
        async with server.lifespan(server.app):
            report['after_init'] = gpu()
            if report['after_init']['free_mib'] < 512:
                report['skipped'] = 'less than 512 MiB free after model initialization'
            else:
                for case in corpus['cases']:
                    images = [decoded[name] for name in case['files']]
                    reference, _, _ = server.recognize(images, case['lang'])
                    runs, text_changes = [], 0
                    for _ in range(args.rounds):
                        started = time.perf_counter()
                        texts, scores, timing = server.recognize(images, case['lang'])
                        runs.append({'wall': time.perf_counter()-started, **timing})
                        text_changes += sum(a != b for a, b in zip(reference, texts))
                    entry = {'id': case['id'], 'kind': case['kind'], 'lang': case['lang'],
                             'lines': len(images), 'texts': reference, 'repeat_text_changes': text_changes,
                             'median_seconds': statistics.median(r['wall'] for r in runs), 'runs': runs}
                    report['cases'].append(entry)
                    print(json.dumps({k: entry[k] for k in ('id','lines','median_seconds','repeat_text_changes')}), flush=True)
        report['status'] = 'completed'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = str(error)
        raise
    finally:
        done.set()
        telemetry.join(timeout=3)
        report['telemetry'] = {'samples': len(samples),
                               'peak_used_mib': max((s['used_mib'] for s in samples), default=None),
                               'min_free_mib': min((s['free_mib'] for s in samples), default=None)}
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('corpus', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--rounds', type=int, default=3)
    asyncio.run(run(ap.parse_args()))
