"""Small batched text-recognition service for searchable-PDF line alignment."""

import asyncio
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
# RTX 5080 / current small-block client: 1 worker beat 4/6/8 in the
# 2026-09-13 real-crop benchmark. Keep overrideable for other workloads.
WORKERS = int(os.environ.get("LINE_OCR_WORKERS", "1"))
if WORKERS < 1:
    raise ValueError("LINE_OCR_WORKERS must be at least 1")
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
request_executor = None
request_slots = None
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


def _trim_bounds(gray: np.ndarray, threshold: int = 245):
    """Exact old ink bounds without allocating coordinates for every pixel."""
    ink = gray < threshold
    ys = np.flatnonzero(ink.any(axis=1))
    xs = np.flatnonzero(ink.any(axis=0))
    if not xs.size:
        return slice(None), slice(None)
    y0, y1 = max(0, int(ys[0]) - 2), min(gray.shape[0], int(ys[-1]) + 3)
    x0, x1 = max(0, int(xs[0]) - 2), min(gray.shape[1], int(xs[-1]) + 3)
    return slice(y0, y1), slice(x0, x1)


def _trim(image: np.ndarray, threshold: int = 245) -> np.ndarray:
    gray = np.dot(image[..., :3], (0.299, 0.587, 0.114))
    return image[_trim_bounds(gray, threshold)]


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
    # Keep float64 dot and thresholds exactly as before. Slicing this same
    # grayscale array avoids recomputing it for the line and each segment.
    gray = np.dot(image[..., :3], (0.299, 0.587, 0.114))
    bounds = _trim_bounds(gray)
    image, gray = image[bounds], gray[bounds]
    height, width = image.shape[:2]
    max_width = max(96, int(round(height * MAX_SEGMENT_RATIO)))
    if width <= max_width:
        return [image], []

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
        segments.append(image[:, left:cut][_trim_bounds(gray[:, left:cut])])
        separators.append(" " if b - a >= word_gap else "")
        left = cut
    segments.append(image[:, left:][_trim_bounds(gray[:, left:])])
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
                           "predictSeconds": predict_seconds, "model": MODELS[lang][0],
                           "workersUsed": worker_count}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor, request_executor, request_slots, ready
    executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="line-rec")
    # A separate coordinator avoids waiting on futures from the GPU pool itself
    # (a deadlock with one worker). Only one request runs inference at a time.
    request_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="line-request")
    request_slots = asyncio.Semaphore(2)  # one running + at most one queued
    try:
        barrier = threading.Barrier(WORKERS)
        futures = [executor.submit(_initialize_worker, barrier) for _ in range(WORKERS)]
        for future in futures:
            future.result()
        ready = True
        yield
    finally:
        ready = False
        request_executor.shutdown(wait=True)
        executor.shutdown(wait=True)
        executor = request_executor = request_slots = None


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok" if ready else "loading", "model": MODEL_NAME,
            "models": {lang: name for lang, (name, _) in MODELS.items()},
            "workers": WORKERS if ready else 0, "batchSize": BATCH_SIZE,
            "inputWidth": INPUT_WIDTH, "device": DEVICE}


@app.post("/recognize")
async def recognize_endpoint(request: RecognitionRequest):
    started = time.perf_counter()
    if not request.images or len(request.images) > MAX_IMAGES:
        raise HTTPException(400, f"images must contain 1 to {MAX_IMAGES} entries")
    lang = request.lang or "default"
    if lang not in MODELS:
        raise HTTPException(400, f"lang must be one of {sorted(MODELS)}")
    if not ready or request_executor is None:
        raise HTTPException(503, "recognizer is loading")
    if request_slots.locked():
        raise HTTPException(429, "recognizer queue is full", headers={"Retry-After": "1"})
    await request_slots.acquire()
    loop = asyncio.get_running_loop()
    try:
        work = loop.run_in_executor(request_executor, _recognize_request, request, lang, started)
    except BaseException:
        request_slots.release()
        raise
    # Cancellation must not admit extra work while a cancelled HTTP caller's
    # GPU job is still running. Release only when the actual work completes.
    slots = request_slots
    released = False
    def release_slot(future):
        nonlocal released
        if not released:
            released = True
            slots.release()
        if not future.cancelled():
            future.exception()  # consume errors even if the HTTP caller left
    work.add_done_callback(release_slot)
    try:
        return await asyncio.shield(work)
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex
    except Exception as ex:
        raise HTTPException(500, f"recognition failed: {ex}") from ex
    finally:
        if work.done():
            release_slot(work)


def _recognize_request(request, lang, started):
    decoding_at = time.perf_counter()
    images = [_decode_image(encoded) for encoded in request.images]
    decoded_at = time.perf_counter()
    texts, scores, timing = recognize(images, lang)
    timing.update(queueSeconds=decoding_at-started,
                  decodeSeconds=decoded_at-decoding_at,
                  requestSeconds=time.perf_counter()-started)
    return {"texts": texts, "scores": scores, "model": MODELS[lang][0], "timing": timing}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081)
