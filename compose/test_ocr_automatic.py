import base64
import contextlib
import io
import json
from collections import Counter
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import pymupdf as fitz
import ocr_to_searchable_pdf as ocr
import ocr_workflow


class AutomaticTests(unittest.TestCase):
    def test_transient_api_errors_retry_but_bad_requests_do_not(self):
        good=Mock()
        good.json.return_value={'result':{'ok':True}}
        with patch.object(ocr.requests,'post',side_effect=[ocr.requests.Timeout(),good]) as call, patch.object(ocr.time,'sleep'):
            self.assertEqual(ocr._call_api(b'image',1),{'ok':True})
            self.assertEqual(call.call_count,2)
        bad=Mock()
        response=Mock(status_code=400)
        bad.raise_for_status.side_effect=ocr.requests.HTTPError(response=response)
        with patch.object(ocr.requests,'post',return_value=bad) as call:
            with self.assertRaises(ocr.requests.HTTPError):
                ocr._call_api(b'image',1)
            self.assertEqual(call.call_count,1)

    def test_healthy_service_does_not_start_docker(self):
        service=ocr_workflow.Service(ocr.BASE,Path(__file__).parent)
        with patch.object(service,'healthy',return_value=True), patch.object(ocr_workflow.subprocess,'run') as run:
            service.ensure()
            service.ensure()
            run.assert_not_called()

    def test_recover_restarts_only_unresponsive_pipeline_api(self):
        service=ocr_workflow.Service(ocr.BASE,Path(__file__).parent)
        with patch.object(service,'healthy',side_effect=[False,True]), \
             patch.object(ocr_workflow.shutil,'which',return_value='docker'), \
             patch.object(ocr_workflow.subprocess,'run') as run:
            service.recover()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0],
                         ['docker','compose','restart','paddleocr-vl-api'])
        self.assertTrue(service.ready)

    def test_ensure_recovers_running_but_unresponsive_pipeline(self):
        service=ocr_workflow.Service(ocr.BASE,Path(__file__).parent)
        probe=Mock(returncode=0)
        running=Mock(returncode=0,stdout='paddleocr-vlm-server\npaddleocr-vl-api\n')
        with patch.object(service,'healthy',return_value=False), \
             patch.object(service,'recover') as recover, \
             patch.object(ocr_workflow.shutil,'which',return_value='docker'), \
             patch.object(ocr_workflow.Path,'exists',return_value=False), \
             patch.object(ocr_workflow.subprocess,'run',side_effect=[probe,running]):
            service.ensure()
        recover.assert_called_once_with()

    def test_custom_service_restarts_its_own_container(self):
        service=ocr_workflow.Service('http://localhost:8081',Path(__file__).parent,
                                     compose_service='paddleocr-line-api')
        with patch.object(service,'healthy',side_effect=[False,True]), \
             patch.object(ocr_workflow.shutil,'which',return_value='docker'), \
             patch.object(ocr_workflow.subprocess,'run') as run:
            service.recover()
        self.assertEqual(run.call_args.args[0],
                         ['docker','compose','restart','paddleocr-line-api'])

    def test_same_job_lock_rejects_another_writer(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'job.lock'
            with ocr_workflow.job_lock(path):
                with self.assertRaisesRegex(RuntimeError,'already being processed'):
                    with ocr_workflow.job_lock(path):
                        pass
            with ocr_workflow.job_lock(path):
                pass

    def test_existing_text_is_not_inserted_twice(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.pdf'; out=Path(td)/'out.pdf'
            with fitz.open() as doc:
                p=doc.new_page()
                p.insert_text((20,30),'Already searchable')
                doc.save(src)
            pr={'width':595,'height':842,'parsing_res_list':[
                {'block_content':'Already searchable','block_bbox':[20,20,200,40]}]}
            report=ocr.overlay(src,[pr],out,automatic=True)
            ocr_workflow.verify(src,out,report)
            with fitz.open(out) as doc:
                self.assertEqual(doc[0].get_text().count('Already searchable'),1)
            self.assertFalse(report['validation_failed'])
            self.assertIn('existing_text_preserved',report['pages'][0]['warnings'])

    def scanned(self, path, pages):
        with fitz.open() as template:
            p = template.new_page(width=240, height=120)
            p.insert_text((20,40),'Alpha beta gamma',fontsize=10)
            p.insert_text((20,60),'Delta epsilon zeta',fontsize=10)
            png = p.get_pixmap(matrix=fitz.Matrix(2,2)).tobytes('png')
        with fitz.open() as doc:
            for _ in range(pages):
                p = doc.new_page(width=240,height=120)
                p.insert_image(p.rect,stream=png)
            doc.save(path)

    def responder(self):
        crops = []
        batches = []
        def row(text):
            return {'markdown':{'text':text}, 'prunedResult':{
                'width':240,'height':120,'parsing_res_list':[
                    {'block_content':text,'block_bbox':[15,25,220,65]}]}}
        def call(data, file_type, *args, **kwargs):
            if file_type == 0:
                with fitz.open(stream=data,filetype='pdf') as doc:
                    batches.append(len(doc))
                    return {'layoutParsingResults':[row('Alpha beta gamma\nDelta epsilon zeta') for _ in doc]}
            raise AssertionError('Unexpected image request to the full document model')
        def line_call(images, timeout=120, lang=None):
            assert lang is None, 'Latin-only blocks must stay on the default recognizer'
            values = ['Alpha beta gamma','Delta epsilon zeta']
            start = len(crops)
            crops.extend(images)
            return [values[(start+i)%2] for i in range(len(images))]
        return call, line_call, crops, batches

    def run_cli(self, src, api, line_api, *extra):
        with patch('sys.argv',['ocr',str(src),*extra]), patch.object(ocr,'_call_api',side_effect=api), \
             patch.object(ocr,'recognize_lines',side_effect=line_api), \
             patch.object(ocr_workflow.Service,'ensure'), patch.object(ocr_workflow.Service,'recover'), \
             patch.object(ocr.requests,'get') as health, contextlib.redirect_stdout(io.StringIO()):
            health.return_value.json.return_value = {'errorMsg':'Healthy'}
            ocr.main()

    def test_300_pages_default_pipeline_and_cache_only_repeat(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'
            self.scanned(src,300)
            call,line_call,crops,batches = self.responder()
            self.run_cli(src,call,line_call)
            self.assertEqual(batches,[10]*30)
            self.assertEqual(len(crops),2)
            out = src.parent/'ocr_output'
            report = json.loads((out/'book_auto_report.json').read_text(encoding='utf-8'))
            self.assertEqual(len(report['pages']),300)
            self.assertFalse(report['validation_failed'])
            self.assertEqual(report['status'],'completed')
            with fitz.open(out/'book_auto_searchable.pdf') as doc:
                self.assertEqual(len(doc),300)
            self.run_cli(src,lambda *a,**kw: self.fail('Unexpected API call on cached repeat'),
                         lambda *a,**kw: self.fail('Unexpected line OCR call on cached repeat'))

    def test_a_font_swapping_one_glyph_codepoint_does_not_fail_the_book(self):
        """A wave dash read back as a fullwidth tilde discarded a 216-page book."""
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td)/'in.pdf', Path(td)/'out.pdf'
            with fitz.open() as doc:
                doc.new_page(width=200, height=80)
                doc.save(src)
            with fitz.open(src) as doc:
                page = doc[0]
                page.insert_font(fontname='malgun', fontfile=r'C:\Windows\Fonts\malgun.ttf')
                page.insert_text((10, 40), '3～5', fontname='malgun', render_mode=3)
                doc.save(out)

            report = {'pages': [{'page': 1, 'warnings': [],
                                 'expected_characters': Counter('3〜5')}]}
            ocr_workflow.verify(src, out, report, debug=True)
            page_report = report['pages'][0]
            self.assertNotIn('extracted_text_mismatch', page_report['warnings'])
            self.assertIn('glyph_equivalent_substituted', page_report['warnings'])
            self.assertFalse(report['validation_failed'])

            # A character that really went missing must still be fatal.
            report = {'pages': [{'page': 1, 'warnings': [],
                                 'expected_characters': Counter('3〜58')}]}
            ocr_workflow.verify(src, out, report, debug=True)
            self.assertIn('extracted_text_mismatch', report['pages'][0]['warnings'])
            self.assertEqual(report['pages'][0]['missing'], {'8': 1})
            self.assertTrue(report['validation_failed'])

    def test_debug_run_keeps_its_own_status_and_report(self):
        """A diagnostic run must not overwrite the finished PDF's status."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'
            self.scanned(src,2)
            out = src.parent/'ocr_output'
            call,line_call,crops,batches = self.responder()
            self.run_cli(src,call,line_call)
            real = json.loads((out/'book_auto_status.json').read_text(encoding='utf-8'))
            self.assertTrue(real['output'].endswith('book_auto_searchable.pdf'))

            call,line_call,crops,batches = self.responder()
            self.run_cli(src,call,line_call,'--debug-lines')
            self.assertTrue((out/'book_auto_debug.pdf').exists())
            self.assertTrue((out/'book_auto_debug_status.json').exists())
            self.assertTrue((out/'book_auto_debug_report.json').exists())
            self.assertEqual(json.loads((out/'book_auto_status.json').read_text(encoding='utf-8')), real)

    def test_crop_failure_resumes_without_losing_previous_final_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'
            self.scanned(src,1)
            out = src.parent/'ocr_output'
            out.mkdir()
            final = out/'book_auto_searchable.pdf'
            final.write_bytes(b'previous final stays intact')
            call,line_call,crops,batches = self.responder()
            def interrupted(images,timeout=120):
                raise RuntimeError('interrupted')
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                self.run_cli(src,call,interrupted)
            self.assertEqual(final.read_bytes(),b'previous final stays intact')
            self.assertEqual(json.loads((out/'book_auto_status.json').read_text())['status'],'failed')
            self.run_cli(src,call,line_call)
            self.assertEqual(batches,[1])
            self.assertEqual(len(crops),2)
            self.assertTrue(final.read_bytes().startswith(b'%PDF'))

    def test_invalid_checkpoint_cannot_shift_pages(self):
        with tempfile.TemporaryDirectory() as td:
            cp=Path(td)/'partial.json'
            ocr.atomic_json(cp,{'identity':{},'batch':10,'pages':[{}],'markdown':['one']})
            with patch.object(ocr,'split_pdf',return_value=iter([(0,b'x',300)])):
                with self.assertRaisesRegex(RuntimeError,'Incomplete batch'):
                    ocr.run_ocr(Path('unused'),10,cp,{})

    def test_validation_detects_extra_and_missing_text(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.pdf'; out=Path(td)/'out.pdf'
            self.scanned(src,1)
            with fitz.open(src) as doc:
                doc[0].insert_text((20,90),'extra',render_mode=3)
                doc.save(out)
            report={'pages':[{'page':1,'warnings':[],'expected_characters':{'a':1}}]}
            ocr_workflow.verify(src,out,report)
            self.assertTrue(report['validation_failed'])


if __name__ == '__main__':
    unittest.main()
