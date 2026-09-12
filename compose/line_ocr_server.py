"""Small batched text-recognition service for searchable-PDF line alignment."""

import base64
import io
import os
import threading
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from fastapi import FastAPI, HTTPException
from paddleocr import TextRecognition
from PIL import Image
from pydantic import BaseModel


MODEL_NAME = "PP-OCRv5_mobile_rec"
MODEL_DIR = os.environ.get("LINE_OCR_MODEL_DIR", f"/models/{MODEL_NAME}")
# The default recognizer's dictionary contains no Hangul, so Korean lines need a
# model of their own. Its dictionary also covers Latin and digits, which keeps
# mixed Korean/English lines readable on it.
KOREAN_MODEL_NAME = "korean_PP-OCRv5_mobile_rec"
KOREAN_MODEL_DIR = os.environ.get("LINE_OCR_KOREAN_MODEL_DIR",
                                  f"/models/{KOREAN_MODEL_NAME}")
MODELS = {"default": (MODEL_NAME, MODEL_DIR),
          "korean": (KOREAN_MODEL_NAME, KOREAN_MODEL_DIR)}
INPUT_WIDTH = int(os.environ.get("LINE_OCR_INPUT_WIDTH", "320"))
BATCH_SIZE = int(os.environ.get("LINE_OCR_BATCH_SIZE", "32"))
CPU_THREADS = int(os.environ.get("LINE_OCR_CPU_THREADS", "8"))
DEVICE = os.environ.get("LINE_OCR_DEVICE", "gpu:0")
WORKERS = int(os.environ.get("LINE_OCR_WORKERS", "4"))
MAX_IMAGES = 256
# A crop wider than this many times its height is cut before recognition. The
# recognizer pads every image in a batch out to INPUT_WIDTH, so the ratio is
# kept near INPUT_WIDTH/48 to avoid squeezing a long line into too few pixels.
MAX_SEGMENT_RATIO = float(os.environ.get("LINE_OCR_MAX_SEGMENT_RATIO",
                                        INPUT_WIDTH / 48))
# A blank column run at least this fraction of the line height reads as a word
# space; anything narrower is letter spacing and is rejoined without a space.
WORD_GAP_RATIO = float(os.environ.get("LINE_OCR_WORD_GAP_RATIO", "0.22"))

executor = None
ready = False
worker_state = threading.local()


class RecognitionRequest(BaseModel):
    images: list[str]
    lang: str | None = None


def _decode_image(encoded: str) -> np.ndarray:
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            return np.asarray(image.convert("RGB"))
    except Exception as ex:
        raise ValueError("invalid base64 image") from ex


def _trim(image: np.ndarray, threshold: int = 245) -> np.ndarray:
    gray = np.dot(image[..., :3], (0.299, 0.587, 0.114))
    ys, xs = np.nonzero(gray < threshold)
    if not xs.size:
        return image
    y0, y1 = max(0, int(ys.min()) - 2), min(image.shape[0], int(ys.max()) + 3)
    x0, x1 = max(0, int(xs.min()) - 2), min(image.shape[1], int(xs.max()) + 3)
    return image[y0:y1, x0:x1]


def _blank_runs(mask: np.ndarray, start: int, end: int):
    """Half-open [a, b) runs of at least two blank columns, in ascending order."""
    window = mask[start:end]
    if not window.size:
        return []
    padded = np.concatenate(([False], window, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(start + int(a), start + int(b))
            for a, b in zip(edges[::2], edges[1::2]) if b - a >= 2]


def split_long_line(image: np.ndarray):
    """Trim a line and split overly wide crops inside blank columns only.

    Returns image segments plus separators to insert between adjacent results.
    A cut never passes through ink: characters sliced in half used to be dropped
    by the recognizer, which moved the reported line boundary. When no blank run
    is available the segment is left over-wide instead, so the recognizer only
    squeezes it rather than losing glyphs. A wide gap rejoins with one space, a
    narrow one with none, because narrow gaps are letter spacing inside a word.
    """
    image = _trim(image)
    height, width = image.shape[:2]
    max_width = max(96, int(round(height * MAX_SEGMENT_RATIO)))
    if width <= max_width:
        return [image], []

    gray = np.dot(image[..., :3], (0.299, 0.587, 0.114))
    ink_per_column = (gray < 225).sum(axis=0)
    blank = ink_per_column <= max(1, height // 100)
    word_gap = max(2, int(round(height * WORD_GAP_RATIO)))
    runs = _blank_runs(blank, 0, width)

    segments, separators = [], []
    left = 0
    while width - left > max_width:
        target = left + max_width
        # Usable runs start far enough in to leave a non-trivial segment.
        usable = [r for r in runs if r[0] > left + 8]
        before = [r for r in usable if (r[0] + r[1]) // 2 <= target]
        if before:
            a, b = before[-1]          # the last gap that still fits
        elif usable:
            a, b = usable[0]           # nothing fits: accept an over-wide piece
        else:
            break                      # no gap left at all: keep the remainder
        cut = (a + b) // 2
        segments.append(_trim(image[:, left:cut]))
        separators.append(" " if b - a >= word_gap else "")
        left = cut
    segments.append(_trim(image[:, left:]))
    return segments, separators


def _result_value(result):
    payload = result.json if hasattr(result, "json") else result
    if callable(payload):
        payload = payload()
    data = payload.get("res", payload)
    return str(data.get("rec_text", "")).strip(), float(data.get("rec_score", 0.0))


def _new_model(lang="default"):
    name, directory = MODELS[lang]
    return TextRecognition(
        model_name=name, model_dir=directory, device=DEVICE,
        cpu_threads=CPU_THREADS, enable_mkldnn=True,
        input_shape=(3, 48, INPUT_WIDTH),
    )


def _initialize_worker(barrier):
    worker_state.predictors = {lang: _new_model(lang) for lang in MODELS}
    barrier.wait(timeout=120)


def _predict_on_worker(batch, lang):
    predictors = getattr(worker_state, "predictors", None)
    if predictors is None:
        predictors = worker_state.predictors = {}
    predictor = predictors.get(lang)
    if predictor is None:
        predictor = predictors[lang] = _new_model(lang)
    return list(predictor.predict(input=batch, batch_size=min(BATCH_SIZE, len(batch))))


def recognize(images: list[np.ndarray], lang: str = "default"):
    started = time.perf_counter()
    flat, mappings = [], []
    for image in images:
        segments, separators = split_long_line(image)
        start = len(flat)
        flat.extend(segments)
        mappings.append((start, len(segments), separators))

    split_seconds = time.perf_counter() - started
    predicted_at = time.perf_counter()

    worker_count = min(WORKERS, len(flat))
    chunk_size = (len(flat) + worker_count - 1) // worker_count
    jobs = []
    for worker in range(worker_count):
        start = worker * chunk_size
        stop = min(len(flat), start + chunk_size)
        if start < stop:
            jobs.append((start, executor.submit(_predict_on_worker, flat[start:stop], lang)))
    values = [None] * len(flat)
    for start, future in jobs:
        results = future.result()
        values[start:start + len(results)] = [_result_value(result) for result in results]
    predict_seconds = time.perf_counter() - predicted_at
    texts, scores = [], []
    for start, count, separators in mappings:
        parts = values[start:start + count]
        combined = parts[0][0] if parts else ""
        for separator, (part, _) in zip(separators, parts[1:]):
            combined += separator + part
        texts.append(" ".join(combined.split()))
        scores.append(min((score for _, score in parts), default=0.0))
    return texts, scores, {"segments": len(flat), "splitSeconds": split_seconds,
                           "predictSeconds": predict_seconds, "model": MODELS[lang][0]}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor, ready
    executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="line-rec")
    barrier = threading.Barrier(WORKERS)
    futures = [executor.submit(_initialize_worker, barrier) for _ in range(WORKERS)]
    for future in futures:
        future.result()
    ready = True
    yield
    ready = False
    executor.shutdown(wait=True)
    executor = None


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok" if ready else "loading", "model": MODEL_NAME,
            "models": {lang: name for lang, (name, _) in MODELS.items()},
            "workers": WORKERS if ready else 0}


@app.post("/recognize")
async def recognize_endpoint(request: RecognitionRequest):
    if not request.images or len(request.images) > MAX_IMAGES:
        raise HTTPException(400, f"images must contain 1 to {MAX_IMAGES} entries")
    lang = request.lang or "default"
    if lang not in MODELS:
        raise HTTPException(400, f"lang must be one of {sorted(MODELS)}")
    try:
        images = [_decode_image(encoded) for encoded in request.images]
        decoded_at = time.perf_counter()
        texts, scores, timing = recognize(images, lang)
        timing["requestSeconds"] = time.perf_counter() - decoded_at
        return {"texts": texts, "scores": scores, "model": MODELS[lang][0],
                "timing": timing}
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex
    except Exception as ex:
        raise HTTPException(500, f"recognition failed: {ex}") from ex


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081)
