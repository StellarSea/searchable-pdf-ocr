import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load_server(workers=None):
    spec = importlib.util.spec_from_file_location('worker_server_test', ROOT/'compose/line_ocr_server.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'paddleocr': types.SimpleNamespace(TextRecognition=Mock())}), \
         patch.dict(os.environ):
        os.environ.pop('LINE_OCR_WORKERS', None)
        if workers is not None:
            os.environ['LINE_OCR_WORKERS'] = str(workers)
        spec.loader.exec_module(module)
    return module


class WorkerTuningTests(unittest.TestCase):
    def test_measured_default_override_and_invalid_count(self):
        self.assertEqual(load_server().WORKERS, 1)
        self.assertEqual(load_server(8).WORKERS, 8)
        with self.assertRaisesRegex(ValueError, 'at least 1'):
            load_server(0)

    def test_results_and_order_are_independent_of_worker_count(self):
        for workers in (1, 2, 4, 6, 8):
            with self.subTest(workers=workers):
                server = load_server(workers)
                images = [np.full((2, 2, 3), i, dtype=np.uint8) for i in range(19)]
                def predict(batch, lang):
                    return [{'rec_text': f'line-{int(image[0,0,0])}', 'rec_score': .99}
                            for image in batch]
                with ThreadPoolExecutor(max_workers=workers) as executor, \
                     patch.object(server, 'executor', executor), \
                     patch.object(server, 'split_long_line', side_effect=lambda image: ([image], [])), \
                     patch.object(server, '_predict_on_worker', side_effect=predict):
                    texts, scores, timing = server.recognize(images, 'korean')
                self.assertEqual(texts, [f'line-{i}' for i in range(19)])
                self.assertEqual(scores, [.99]*19)
                self.assertEqual(timing['workersUsed'], workers)

    def test_health_reports_actual_scheduling_and_input_settings(self):
        server = load_server(2)
        server.ready = True
        health = asyncio.run(server.health())
        self.assertEqual(health['workers'], 2)
        self.assertEqual(health['inputWidth'], 320)
        self.assertEqual(health['batchSize'], 32)


if __name__ == '__main__':
    unittest.main()
