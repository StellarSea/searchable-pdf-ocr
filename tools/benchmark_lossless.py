"""Paired old/new preprocessing benchmark on explicit real-crop corpora.

CPU-only by default. --gpu runs in an isolated line-service container, using
one set of warmed predictors for both variants. Never changes production data.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
import types
from unittest.mock import patch

import numpy as np
from PIL import Image


def load(path, name, gpu):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if gpu:
        spec.loader.exec_module(module)
    else:
        with patch.dict(sys.modules, {'paddleocr': types.SimpleNamespace(TextRecognition=None)}):
            spec.loader.exec_module(module)
    return module


async def run(args):
    before = load(args.baseline, 'old_split_server', args.gpu)
    after = load(args.server, 'new_split_server', args.gpu)
    cases, decoded = [], []
    for corpus_path in args.corpus:
        corpus = json.loads(corpus_path.read_text(encoding='utf-8'))
        for case in corpus['cases']:
            if case['kind'] != 'block':
                continue
            images = []
            for name in case['files']:
                with Image.open(corpus_path.parent/name) as image:
                    images.append(np.asarray(image.convert('RGB')))
            cases.append((f'{corpus_path.parent.name}/{case["id"]}', case['lang'], images))
            decoded.extend(images)
    for image in decoded:
        a, b = before.split_long_line(image), after.split_long_line(image)
        assert a[1] == b[1] and len(a[0]) == len(b[0]), 'Changed segment count/separator'
        for x, y in zip(a[0], b[0]):
            assert x.shape == y.shape and np.array_equal(x, y), 'Changed segment pixels'
    timings = {'before': [], 'after': []}
    for round_index in range(args.rounds):
        order = [('before', before), ('after', after)]
        if round_index % 2:
            order.reverse()
        for label, module in order:
            start = time.perf_counter()
            for image in decoded:
                module.split_long_line(image)
            timings[label].append(time.perf_counter()-start)
    report = {'lines': len(decoded), 'exact_segment_pixels_and_separators': True,
              'rounds': args.rounds, 'preprocess_seconds': timings,
              'preprocess_median': {k: statistics.median(v) for k, v in timings.items()},
              'corpora': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in args.corpus},
              'gpu': []}
    if args.gpu:
        optimized_split = after.split_long_line
        async with after.lifespan(after.app):
            for name, lang, images in cases:
                after.recognize(images, lang)  # warm the same model and shapes
                values = {'before': [], 'after': []}
                reference = None
                for round_index in range(args.rounds):
                    order = [('before', before.split_long_line), ('after', optimized_split)]
                    if round_index % 2:
                        order.reverse()
                    for label, splitter in order:
                        after.split_long_line = splitter
                        start = time.perf_counter()
                        texts, scores, timing = after.recognize(images, lang)
                        values[label].append({'wall': time.perf_counter()-start, **timing})
                        if reference is None:
                            reference = (texts, scores)
                        assert (texts, scores) == reference, f'Changed text, order, or score: {name}'
                report['gpu'].append({'case': name, 'lines': len(images), 'exact_texts_scores': True,
                                      'runs': values})
                print(json.dumps({'case': name, 'exact': True}), flush=True)
        after.split_long_line = optimized_split
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('gpu', 'corpora')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus', type=Path, nargs='+')
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--server', type=Path, default=Path(__file__).resolve().parents[1]/'compose/line_ocr_server.py')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--gpu', action='store_true')
    asyncio.run(run(parser.parse_args()))
