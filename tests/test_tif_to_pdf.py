"""Offline TIFF resolution regressions; every PDF and scan is temporary."""
import contextlib
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
import io
from pathlib import Path
import sys
import tempfile
import unittest
import warnings

import pymupdf
from PIL import Image, TiffImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from tif_to_pdf import (choose_workers, convert_tiffs, insert_lossless_image,
                       main, plan_frames, prepared_frames, read_dpi)


class TiffDpiTests(unittest.TestCase):
    def write_tiff(self, path, frames):
        buffer = io.BytesIO()
        with TiffImagePlugin.AppendingTiffWriter(buffer) as writer:
            for image, metadata in frames:
                tags = TiffImagePlugin.ImageFileDirectory_v2()
                for key, value in metadata.items():
                    tags[key] = value
                image.save(writer, format='TIFF', tiffinfo=tags)
                writer.newFrame()
        writer.close()
        path.write_bytes(buffer.getvalue())

    def convert(self, source, output, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return convert_tiffs([source], output, **kwargs)

    def assert_page(self, page, pixels, dpi):
        self.assertAlmostEqual(page.rect.width, pixels[0] * 72 / dpi[0], places=4)
        self.assertAlmostEqual(page.rect.height, pixels[1] * 72 / dpi[1], places=4)
        self.assertEqual(page.rotation, 0)
        self.assertEqual(page.cropbox, page.mediabox)
        images = page.get_images(full=True)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0][2:4], pixels)
        rect = page.get_image_bbox(images[0])
        for actual, expected in zip(rect, page.rect):
            self.assertAlmostEqual(actual, expected, places=4)
        return images[0][0]

    def test_600dpi_page_size_and_embedded_jpeg_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = Path(td)/'scan.tif', Path(td)/'scan.pdf'
            image = Image.new('RGB', (600, 300), 'white')
            self.write_tiff(source, [(image, {282: 600, 283: 600, 296: 2})])
            self.assertEqual(self.convert(source, output), 1)
            with pymupdf.open(output) as doc:
                xref = self.assert_page(doc[0], (600, 300), (600, 600))
                with Image.open(io.BytesIO(doc.extract_image(xref)['image'])) as jpeg:
                    self.assertEqual(jpeg.info['dpi'], (600, 600))

    def test_each_frame_uses_its_own_axes_and_units(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = Path(td)/'scan.tif', Path(td)/'scan.pdf'
            image = Image.new('RGB', (120, 60), 'white')
            self.write_tiff(source, [
                (image, {282: 600, 283: 300, 296: 2}),
                (image, {282: 100, 283: 200, 296: 3}),
            ])
            for lossless in (False, True):
                with self.subTest(lossless=lossless):
                    self.assertEqual(self.convert(source, output, lossless=lossless), 2)
                    with pymupdf.open(output) as doc:
                        for i, dpi in enumerate([(600, 300), (254, 508)]):
                            xref = self.assert_page(doc[i], image.size, dpi)
                            if not lossless:
                                with Image.open(io.BytesIO(doc.extract_image(xref)['image'])) as jpeg:
                                    self.assertEqual(jpeg.info['dpi'], dpi)

    def test_palette_conversion_preserves_geometry_and_lossless_pixels(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = Path(td)/'scan.tif', Path(td)/'scan.pdf'
            image = Image.new('P', (120, 60))
            image.putpalette([0, 0, 0, 255, 100, 20] + [0]*762)
            image.paste(1, (10, 5, 100, 50))
            self.write_tiff(source, [(image, {282: 300, 283: 150, 296: 2})])
            self.convert(source, output, lossless=True)
            with pymupdf.open(output) as doc:
                xref = self.assert_page(doc[0], image.size, (300, 150))
                pix = pymupdf.Pixmap(doc, xref)
                self.assertEqual(pix.samples, image.convert('RGB').tobytes())
                # Render back to the source's pixel grid, including unequal DPI axes.
                rendered = doc[0].get_pixmap(matrix=pymupdf.Matrix(300/72, 150/72))
                self.assertEqual((rendered.width, rendered.height), image.size)
                self.assertEqual(rendered.samples, image.convert('RGB').tobytes())

    def test_lossless_pages_are_compressed_before_final_save(self):
        # Distinct pages plus a repeated page exercise image reuse, order and
        # exact pixels. Inspect before save: final compression hid this bug.
        for mode in ('RGB', 'L'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                images = []
                with pymupdf.open() as doc:
                    for i in (1, 2, 3, 1):
                        image = Image.new(mode, (600, 300), 'white')
                        image.paste(0, (i * 20, 10, i * 20 + 10, 200))
                        images.append(image)
                        page = doc.new_page(width=72, height=36)
                        xref = insert_lossless_image(doc, page, image)
                        self.assertLess(len(doc.xref_stream_raw(xref)),
                                        len(image.tobytes()) // 10)
                        self.assertEqual(pymupdf.Pixmap(doc, xref).samples,
                                         image.tobytes())
                    output = Path(td) / 'compressed.pdf'
                    # No final deflate: the page loop must have done the work.
                    doc.save(output, deflate=False)
                with pymupdf.open(output) as doc:
                    self.assertEqual(len(doc), len(images))
                    for page, image in zip(doc, images):
                        xref = self.assert_page(page, image.size, (600, 600))
                        self.assertEqual(pymupdf.Pixmap(doc, xref).samples,
                                         image.tobytes())
                        rendered = page.get_pixmap(matrix=pymupdf.Matrix(600/72, 600/72))
                        self.assertEqual(rendered.samples, image.convert('RGB').tobytes())

    def test_missing_or_unitless_resolution_warns_and_defaults_to_600(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = Path(td)/'scan.tif', Path(td)/'scan.pdf'
            for metadata in ({}, {282: 300, 283: 300, 296: 1},
                             {282: 0, 283: 600, 296: 2}, {282: 600, 296: 2}):
                with self.subTest(metadata=metadata):
                    self.write_tiff(source, [(Image.new('L', (60, 30)), metadata)])
                    with self.assertWarnsRegex(UserWarning, 'assuming 600 x 600'):
                        self.convert(source, output)
                    with pymupdf.open(output) as doc:
                        self.assert_page(doc[0], (60, 30), (600, 600))

    def test_omitted_resolution_unit_uses_tiff_default_inches(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td)/'scan.tif'
            self.write_tiff(source, [(Image.new('L', (60, 30)), {282: 300, 283: 150})])
            with Image.open(source) as image:
                self.assertEqual(read_dpi(image), (300, 150))

    def test_explicit_override_applies_to_pdf_and_jpeg(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = Path(td)/'scan.tif', Path(td)/'scan.pdf'
            self.write_tiff(source, [(Image.new('L', (60, 30)), {})])
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                self.convert(source, output, dpi=300)
            self.assertEqual(caught, [])
            with pymupdf.open(output) as doc:
                xref = self.assert_page(doc[0], (60, 30), (300, 300))
                with Image.open(io.BytesIO(doc.extract_image(xref)['image'])) as jpeg:
                    self.assertEqual(jpeg.info['dpi'], (300, 300))

    def test_invalid_cli_override_fails_before_creating_output(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)/'scan.pdf'
            for dpi in ('0', '-1', 'nan', 'inf'):
                with self.subTest(dpi=dpi), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        main(['missing.tif', str(output), '--dpi', dpi])
                    self.assertEqual(error.exception.code, 2)
                    self.assertFalse(output.exists())

    def test_spawn_matches_serial_for_mixed_frames_and_jpeg(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rgb = Image.new('RGB', (120, 60), (120, 30, 240))
            gray = Image.new('L', (90, 45), 180)
            gray.paste(0, (10, 5, 60, 30))
            bw = Image.new('1', (60, 30), 1)
            bw.paste(0, (5, 5, 20, 20))
            palette = Image.new('P', (120, 60), 1)
            palette.putpalette([0, 0, 0, 250, 80, 10] + [0]*762)
            source1, source2 = root/'01.tif', root/'02.tif'
            self.write_tiff(source1, [(rgb, {282:600, 283:300, 296:2}),
                                      (gray, {282:100, 283:200, 296:3})])
            self.write_tiff(source2, [(bw, {}), (palette, {282:300, 283:300, 296:2})])
            sources = [source1, source2]
            jobs = plan_frames(sources)
            self.assertEqual([(Path(j[0]).name, j[1]) for j in jobs],
                             [('01.tif',0), ('01.tif',1), ('02.tif',0), ('02.tif',1)])
            for lossless, override in ((True, None), (False, None), (True, 450)):
                with self.subTest(lossless=lossless, override=override):
                    serial, parallel = root/'serial.pdf', root/'parallel.pdf'
                    with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
                        warnings.simplefilter('ignore')
                        convert_tiffs(sources, serial, dpi=override, lossless=lossless, workers=1)
                    messages = []
                    with pymupdf.open() as doc:
                        for data, notes in prepared_frames(jobs, 2, override, 88, lossless):
                            messages.extend(notes)
                            with pymupdf.open(stream=data, filetype='pdf') as part:
                                doc.insert_pdf(part)
                        doc.save(parallel)
                    self.assertEqual(len(messages), 0 if override else 1)
                    with pymupdf.open(serial) as a, pymupdf.open(parallel) as b:
                        self.assertEqual(len(a), len(b))
                        for pa, pb in zip(a, b):
                            self.assertEqual(pa.rect, pb.rect)
                            self.assertEqual(pa.mediabox, pb.mediabox)
                            self.assertEqual(pa.cropbox, pb.cropbox)
                            self.assertEqual(pb.rotation, 0)
                            xa, xb = pa.get_images()[0][0], pb.get_images()[0][0]
                            self.assertEqual(pymupdf.Pixmap(a, xa).samples,
                                             pymupdf.Pixmap(b, xb).samples)
                            self.assertEqual(pa.get_pixmap().samples, pb.get_pixmap().samples)
                            if not lossless:
                                self.assertEqual(a.extract_image(xa)['image'],
                                                 b.extract_image(xb)['image'])

    def test_worker_budget_limits_by_memory_cores_and_page_count(self):
        mib = 1024**2
        jobs = [('scan.tif', i, 512*mib) for i in range(10)]
        self.assertEqual(choose_workers(jobs, 0, 4096, available=20*1024*mib, cpu_count=16), 4)
        self.assertEqual(choose_workers(jobs, 8, 1024, available=20*1024*mib, cpu_count=16), 2)
        self.assertEqual(choose_workers(jobs, 8, 4096, available=512*mib, cpu_count=16), 1)
        self.assertEqual(choose_workers(jobs, 8, 4096, available=20*1024*mib, cpu_count=2), 2)
        self.assertEqual(choose_workers(jobs[:1], 8, 4096, available=20*1024*mib, cpu_count=16), 1)

    def test_bounded_out_of_order_preparation_and_failure_recovery(self):
        for fail_second in (False, True):
            with self.subTest(fail_second=fail_second):
                outstanding = []
                prepared = []
                maximum = 0

                def prepare(job, *args):
                    prepared.append(job)
                    return job

                class Pool:
                    def __init__(self, **kwargs):
                        self.width = kwargs['max_workers']

                    def submit(self, fn, job, *args):
                        nonlocal maximum
                        future = Future()
                        outstanding.append((future, fn, job, args))
                        maximum = max(maximum, len(outstanding))
                        self.flush() if len(outstanding) == self.width or job == 6 else None
                        return future

                    def flush(self):
                        # Deliberately finish later source pages first.
                        for future, fn, job, args in reversed(outstanding):
                            if future.cancelled():
                                continue
                            if fail_second and job == 1:
                                future.set_exception(BrokenProcessPool('test worker exit'))
                            else:
                                future.set_result(fn(job, *args))
                        outstanding.clear()

                    def shutdown(self, **kwargs):
                        self.flush()

                with warnings.catch_warnings(record=True) as caught:
                    result = list(prepared_frames(list(range(7)), 2, None, 88, True,
                                                  executor_factory=Pool, prepare=prepare))
                self.assertEqual(result, list(range(7)))
                self.assertLessEqual(maximum, 2)
                self.assertEqual(len(caught), int(fail_second))
                self.assertEqual(prepared.count(0), 1)

    def test_invalid_parallel_cli_options_do_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)/'scan.pdf'
            for flag, value in (('--workers', '-1'), ('--memory-mb', '0')):
                with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        main(['missing.tif', str(output), flag, value])
                    self.assertFalse(output.exists())

    def test_pool_startup_failure_uses_serial_preparation(self):
        def unavailable(**kwargs):
            raise OSError('process creation unavailable')

        with self.assertWarnsRegex(UserWarning, 'continuing sequentially'):
            self.assertEqual(list(prepared_frames([1, 2, 3], 2, None, 88, True,
                             executor_factory=unavailable, prepare=lambda job, *args: job)),
                             [1, 2, 3])


if __name__ == '__main__':
    unittest.main()
