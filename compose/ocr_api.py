"""OCR HTTP transport and model/cache identities. No job orchestration or server startup."""
import base64
import time
import requests


BASE = "http://127.0.0.1:8080"

API = f"{BASE}/layout-parsing"

LINE_BASE = "http://127.0.0.1:8081"

LINE_API = f"{LINE_BASE}/recognize"

LINE_REQUEST_LIMIT = 256

# Historical cache namespaces, not the current worker count. Scheduling-only
# tuning must not invalidate image responses or image-bound manual reviews.

LINE_ENGINE = "PP-OCRv5_mobile_rec-gpu-320-w4-v2"

LINE_ENGINE_KOREAN = "korean_PP-OCRv5_mobile_rec-gpu-320-w4-v2"

API_OPTIONS = {"visualize": False, "useChartRecognition": False,
               "useSealRecognition": False, "mergeLayoutBlocks": False, "layoutNms": True}

def _call_api(data: bytes, file_type: int, timeout=600, options=None, attempts=3) -> dict:
    payload = {
        "file": base64.b64encode(data).decode(),
        "fileType": file_type,
        **API_OPTIONS,
        **(options or {}),
    }
    for attempt in range(attempts):
        try:
            r = requests.post(API, json=payload, timeout=(10, timeout or 600))
            r.raise_for_status()
            break
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as ex:
            status = getattr(getattr(ex, 'response', None), 'status_code', None)
            if attempt == attempts - 1 or (status is not None and status not in (429,500,502,503,504)):
                raise
            print(f'[retry] API attempt {attempt+2}/{attempts}', flush=True)
            time.sleep(2**(attempt+1))
    body = r.json()
    if body.get("errorCode", 0) != 0:
        raise RuntimeError(f"API error {body['errorCode']}: {body.get('errorMsg')}")
    return body["result"]

def recognize_lines(png_images, timeout=120, lang=None):
    """Recognize line crops in ordered, size-limited requests to the GPU service."""
    all_values = []
    for start in range(0, len(png_images), LINE_REQUEST_LIMIT):
        batch = png_images[start:start + LINE_REQUEST_LIMIT]
        payload = {'images': [base64.b64encode(data).decode() for data in batch]}
        if lang:
            payload['lang'] = lang
        # A responsive but busy service must not trigger an immediate recovery
        # attempt. Keep batch order and payload intact during bounded backoff.
        for attempt in range(3):
            try:
                r = requests.post(LINE_API, json=payload, timeout=(10, timeout))
                r.raise_for_status()
                break
            except requests.HTTPError as ex:
                status = getattr(getattr(ex, 'response', None), 'status_code', None)
                if status not in (429, 503) or attempt == 2:
                    raise
                time.sleep(2**attempt)
        body = r.json()
        values = body.get('texts')
        if not isinstance(values, list) or len(values) != len(batch):
            raise RuntimeError('Line OCR returned an unexpected result count')
        if not all(isinstance(value, str) for value in values):
            raise RuntimeError('Line OCR returned invalid text')
        all_values.extend(values)
    return all_values
