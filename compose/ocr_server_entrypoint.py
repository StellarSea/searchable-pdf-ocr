"""Opt-in PaddleX launcher. OCR_PDF_WORKERS=0 preserves the stock reader."""
import atexit
import os


def decode_with_release(reader, original, data, file_type, *, max_num_imgs=None):
    # Image repair follows the document phase in our OCR client. Do not keep
    # PDF worker interpreters resident through CPU-heavy boundary/overlay work.
    if file_type != 'PDF':
        reader.release()
    return original(data, file_type, max_num_imgs=max_num_imgs)


def install():
    workers = int(os.environ.get('OCR_PDF_WORKERS', '0'))
    if workers == 0:
        return None
    from paddlex.inference.serving.infra import utils
    from ocr_server_pdf import ParallelPDFReader
    reader = ParallelPDFReader(utils.read_pdf, workers=workers,
        memory_mb=int(os.environ.get('OCR_PDF_MEMORY_MB', '8192')),
        timeout=float(os.environ.get('OCR_PDF_TIMEOUT', '60')))
    reader.warmup()
    utils.read_pdf = reader
    from functools import partial
    utils.file_to_images = partial(decode_with_release, reader, utils.file_to_images)
    atexit.register(reader.close)
    return reader


def main():
    reader = install()
    try:
        from paddlex.__main__ import console_entry
        return console_entry()
    finally:
        if reader is not None:
            reader.close()


if __name__ == '__main__':
    raise SystemExit(main())
