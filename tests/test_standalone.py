"""Offline workflow regressions. HTTP and portal searches are mocked; PDFs are synthetic."""
import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = load_module('standalone_scihub_under_test', ROOT / 'scihub_downloader.py')
turkish = load_module('standalone_turkish_under_test', ROOT / 'turkish_literature.py')
DOI = '10.1234/test.article'
MIRROR = 'https://mirror.example'


def make_pdf(doi=DOI):
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f'BT /F1 12 Tf 36 700 Td ({doi}) Tj ET'.encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


PDF = make_pdf()


class Response:
    def __init__(self, url, body=b'', status=200, headers=None):
        self.url, self.body, self.status_code = url, body, status
        self.headers = headers or {}
        self.encoding = 'utf-8'
        self.closed = False

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start+size]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TemporaryWork(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def read_csv(self, path):
        with Path(path).open(encoding='utf-8-sig', newline='') as handle:
            return list(csv.DictReader(handle))

    def chdir(self):
        previous = Path.cwd()
        os.chdir(self.folder)
        self.addCleanup(os.chdir, previous)


class StandalonePdfTests(TemporaryWork):
    def setUp(self):
        super().setUp()
        self.calls = []

    def http(self, responder):
        def request(method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return responder(method, url, kwargs)
        self.stack.enter_context(patch('requests.Session.request', side_effect=request))

    def selected(self, rows=None):
        path = self.folder / 'selected.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, ['Title', 'DOI', 'Notes'], delimiter=';')
            writer.writeheader()
            writer.writerows(rows or [{'Title': 'Göksu kıyısı', 'DOI': DOI, 'Notes': 'preserve'}])
        return path

    def flags(self):
        return ['--mirror', MIRROR, '--scihub-delay', '0', '--pdf-dir', str(self.folder / 'pdfs')]

    def test_csv_and_out_aliases_work_without_subcommand(self):
        selected = self.selected()
        report = self.folder / 'report.csv'
        self.http(lambda method, url, kw: Response(url, PDF))
        code = core.main(['--csv', str(selected), '--out', str(report)] + self.flags())
        self.assertEqual(code, 0)
        row = self.read_csv(report)[0]
        self.assertEqual(row['Notes'], 'preserve')
        self.assertEqual(row['Title'], 'Göksu kıyısı')
        self.assertEqual(row['PDF_status'], 'downloaded')
        self.assertEqual(Path(row['Pdf_path']).read_bytes(), PDF)
        self.assertEqual(len(self.calls), 1)

    def test_positional_doi_runs_without_subcommand(self):
        report = self.folder / 'report.csv'
        self.http(lambda method, url, kw: Response(url, PDF))
        self.assertEqual(core.main([DOI, '--out', str(report)] + self.flags()), 0)
        self.assertEqual(self.calls[0][1], MIRROR + '/' + DOI)

    def test_noargs_uses_selected_csv_in_current_folder(self):
        self.selected()
        self.chdir()
        self.stack.enter_context(patch.dict(os.environ, {'SCIHUB_MIRRORS': MIRROR}))
        self.http(lambda method, url, kw: Response(url, PDF))
        self.assertEqual(core.main([]), 0)
        self.assertEqual(self.read_csv(self.folder / 'pdf_download_report.csv')[0]['DOI'], DOI)

    def test_noargs_can_prompt_for_doi_when_no_selected_file(self):
        self.chdir()
        self.stack.enter_context(patch.dict(os.environ, {'SCIHUB_MIRRORS': MIRROR}))
        self.stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=True))
        self.stack.enter_context(patch('builtins.input', return_value=DOI))
        self.http(lambda method, url, kw: Response(url, PDF))
        self.assertEqual(core.main([]), 0)
        self.assertTrue((self.folder / 'pdf_download_report.csv').is_file())

    def test_duplicate_rows_share_request_but_invalid_doi_is_reported(self):
        selected = self.selected([{'Title': 'One', 'DOI': DOI, 'Notes': 'a'},
                                  {'Title': 'One', 'DOI': 'doi:' + DOI, 'Notes': 'b'},
                                  {'Title': 'Thesis', 'DOI': '', 'Notes': 'no DOI'}])
        report = self.folder / 'report.csv'
        self.http(lambda method, url, kw: Response(url, PDF))
        self.assertEqual(core.main(['--csv', str(selected), '--out', str(report)] + self.flags()), 1)
        rows = self.read_csv(report)
        self.assertEqual([row['Notes'] for row in rows], ['a', 'b', 'no DOI'])
        self.assertEqual(rows[-1]['PDF_status'], 'missing_or_invalid_doi')
        self.assertEqual(len(self.calls), 1)

    def test_failed_mirror_rotates_then_resolves_embedded_pdf(self):
        def response(method, url, kwargs):
            if url.startswith(MIRROR):
                return Response(url, b'Article not found')
            return Response(url, PDF if url.endswith('/paper.pdf') else b'<object data="/paper.pdf#view=FitH"></object>')
        self.http(response)
        with core.SciHubDownloader([MIRROR, 'https://second.example'], self.folder / 'pdfs', delay=0) as downloader:
            result = downloader.download(DOI)
        self.assertEqual(result['PDF_status'], 'downloaded')
        self.assertEqual(result['PDF_URL'], 'https://second.example/paper.pdf')
        self.assertIn('article unavailable', result['PDF_error'])
        self.assertEqual(self.calls[-1][2]['headers']['Referer'], 'https://second.example/' + DOI)

    def test_html_with_pdf_content_type_cannot_be_published(self):
        self.http(lambda method, url, kw: Response(url, b'<html>not a PDF</html>', headers={'Content-Type': 'application/pdf'}))
        with core.SciHubDownloader([MIRROR], self.folder / 'pdfs', delay=0) as downloader:
            with self.assertRaisesRegex(ValueError, 'not a PDF'):
                downloader.save(MIRROR + '/paper.pdf', self.folder / 'pdfs' / 'paper.pdf', DOI)
        self.assertEqual(list((self.folder / 'pdfs').iterdir()), [])

    def test_existing_report_requires_explicit_overwrite(self):
        selected = self.selected()
        report = self.folder / 'report.csv'
        report.write_text('previous report')
        self.http(lambda *args: self.fail('No network before output validation'))
        with self.assertRaises(SystemExit) as raised:
            core.main(['--csv', str(selected), '--out', str(report)] + self.flags())
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(report.read_text(), 'previous report')

    def test_report_cannot_overwrite_selected_input_even_with_flag(self):
        selected = self.selected()
        before = selected.read_bytes()
        self.http(lambda *args: self.fail('No network before input protection'))
        with self.assertRaises(SystemExit) as raised:
            core.main(['--csv', str(selected), '--out', str(selected), '--overwrite'] + self.flags())
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(selected.read_bytes(), before)


class TurkishKeywordTests(TemporaryWork):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch('requests.Session.request', side_effect=AssertionError('Offline test attempted HTTP')))

    def result(self, rows=None, status='completed', error='', total=None):
        return {'rows': rows or [], 'status': status, 'error': error,
                'search_url': 'https://portal.example/search', 'pages_fetched': 1,
                'total_reported': total if total is not None else len(rows or [])}

    def adapter(self, name, **kwargs):
        return self.stack.enter_context(patch.object(turkish, name, **kwargs))

    def outargs(self, sources='yok_tez,tr_dizin,dergipark'):
        return ['--sources', sources, '--delay', '0', '--out', str(self.folder / 'results.csv')]

    def test_keywords_query_all_three_portals_without_input_csv(self):
        yok = self.adapter('search_yok_tez', return_value=self.result([{'Title': 'Tez', 'Thesis_ID': '12'}]))
        tr = self.adapter('search_tr_dizin', return_value=self.result([{'Title': 'Makale', 'DOI': DOI}]))
        dp = self.adapter('search_dergipark', return_value=self.result([]))
        self.assertEqual(turkish.main(['yeraltı suyu'] + self.outargs()), 0)
        for adapter in (yok, tr, dp):
            self.assertEqual(adapter.call_count, 1)
            self.assertEqual(adapter.call_args.args[1], 'yeraltı suyu')
        rows = self.read_csv(self.folder / 'results.csv')
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['Source'] for row in rows}, {'YOKTez', 'TRDizin'})
        self.assertTrue(all(row['Query'] == 'yeraltı suyu' for row in rows))

    def test_keywords_flag_and_sources_limit_searches(self):
        tr = self.adapter('search_tr_dizin', return_value=self.result([]))
        yok = self.adapter('search_yok_tez', side_effect=AssertionError('Unselected source searched'))
        dp = self.adapter('search_dergipark', side_effect=AssertionError('Unselected source searched'))
        code = turkish.main(['--keywords', 'karst', 'Göksu', 'karst'] + self.outargs('tr_dizin'))
        self.assertEqual(code, 0)
        self.assertEqual([call.args[1] for call in tr.call_args_list], ['karst', 'Göksu'])
        yok.assert_not_called()
        dp.assert_not_called()

    def test_blocked_portal_keeps_other_results_and_exit_is_nonzero(self):
        self.adapter('search_yok_tez', return_value=self.result([{'Title': 'Tez', 'Year': '2023'}]))
        self.adapter('search_tr_dizin', return_value=self.result([{'Title': 'Makale', 'DOI': DOI}]))
        self.adapter('search_dergipark', return_value=self.result(status='blocked', error='Human verification required'))
        self.assertEqual(turkish.main(['karst'] + self.outargs()), 1)
        self.assertEqual(len(self.read_csv(self.folder / 'results.csv')), 2)
        audit = json.loads((self.folder / 'results.audit.json').read_text())
        blocked = next(job for job in audit['jobs'] if job['source'] == 'dergipark')
        self.assertEqual(blocked['status'], 'blocked')
        self.assertIn('verification', blocked['error'])
        self.assertEqual(blocked['retrieved'], 0)

    def test_exception_is_failure_and_does_not_claim_zero_result_search(self):
        self.adapter('search_yok_tez', side_effect=RuntimeError('Portal changed HTML structure'))
        self.assertEqual(turkish.main(['karst'] + self.outargs('yok_tez')), 1)
        audit = json.loads((self.folder / 'results.audit.json').read_text())
        self.assertEqual(audit['jobs'][0]['status'], 'failed')
        self.assertIn('Portal changed', audit['jobs'][0]['error'])

    def test_duplicate_doi_preserves_source_query_and_link_provenance(self):
        rows = [{'Title': 'Göksu', 'DOI': DOI.upper(), 'Source': 'TRDizin', 'Query': 'karst', 'Link': 'https://tr.example/1'},
                {'Title': 'Göksu', 'DOI': 'https://doi.org/' + DOI, 'Source': 'DergiPark', 'Query': 'Göksu', 'Link': 'https://dp.example/1'}]
        merged = turkish.merge_results(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['DOI'], DOI)
        self.assertEqual(merged[0]['Source'], 'TRDizin | DergiPark')
        self.assertEqual(merged[0]['Query'], 'karst | Göksu')
        self.assertEqual(merged[0]['Links'], 'https://tr.example/1 | https://dp.example/1')

    def test_distinct_thesis_ids_are_not_merged_by_shared_search_url(self):
        rows = [{'Title': 'Tez A', 'Source': 'YOKTez', 'Thesis_ID': '11', 'Link': 'https://tez.example/results'},
                {'Title': 'Tez B', 'Source': 'YOKTez', 'Thesis_ID': '22', 'Link': 'https://tez.example/results'},
                {'Title': 'Tez A', 'Source': 'YOKTez', 'Thesis_ID': '11', 'Link': 'https://tez.example/results', 'Query': 'karst'}]
        merged = turkish.merge_results(rows)
        self.assertEqual(len(merged), 2)
        self.assertEqual({row['Thesis_ID'] for row in merged}, {'11', '22'})

    def test_capped_search_remains_explicit_in_audit(self):
        self.adapter('search_tr_dizin', return_value=self.result(
            [{'Title': 'One visible result', 'DOI': DOI}], status='capped', total=484))
        self.assertEqual(turkish.main(['yeraltı suyu'] + self.outargs('tr_dizin')), 0)
        audit = json.loads((self.folder / 'results.audit.json').read_text())
        self.assertEqual(audit['jobs'][0]['status'], 'capped')
        self.assertEqual(audit['jobs'][0]['total_reported'], 484)
        self.assertEqual(audit['jobs'][0]['retrieved'], 1)

    def test_later_doi_enrichment_merges_same_portal_record_and_queries(self):
        rows = [{'Title': 'Göksu', 'DOI': '', 'Source': 'DergiPark', 'Query': 'karst',
                 'Link': 'https://dergipark.example/article/12', 'Record_IDs': 'DergiPark:12'},
                {'Title': 'Göksu', 'DOI': 'https://doi.org/' + DOI, 'Source': 'DergiPark', 'Query': 'Göksu',
                 'Link': 'https://dergipark.example/article/12', 'Record_IDs': 'DergiPark:12'}]
        merged = turkish.merge_results(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['DOI'], DOI)
        self.assertEqual(merged[0]['Query'], 'karst | Göksu')
        self.assertEqual(merged[0]['Record_IDs'], 'DergiPark:12')
        self.assertEqual(merged[0]['Links'], 'https://dergipark.example/article/12')

    def test_browser_block_preserves_native_partial_rows_and_attempt_audit(self):
        native = self.result([{'Title': 'Earlier HTTP result', 'DOI': DOI}],
                             status='blocked', error='Page 2 requires verification', total=20)
        self.adapter('search_dergipark', return_value=native)
        browser = self.adapter('search_dergipark_browser', return_value=self.result(
            status='blocked', error='Browser verification timed out', total=None))
        page = object()
        context = types.SimpleNamespace(pages=[page], close=Mock(side_effect=RuntimeError('browser already closed')))
        chromium = types.SimpleNamespace(launch_persistent_context=Mock(return_value=context))
        playwright = types.SimpleNamespace(chromium=chromium, stop=Mock(side_effect=RuntimeError('driver already closed')))
        starter = types.SimpleNamespace(start=Mock(return_value=playwright))
        package = types.ModuleType('playwright')
        sync_api = types.ModuleType('playwright.sync_api')
        sync_api.sync_playwright = Mock(return_value=starter)
        self.stack.enter_context(patch.dict(sys.modules, {'playwright': package, 'playwright.sync_api': sync_api}))
        code = turkish.main(['karst', '--browser', '--browser-profile', str(self.folder / 'browser-profile')]
                            + self.outargs('dergipark'))
        self.assertEqual(code, 1)
        rows = self.read_csv(self.folder / 'results.csv')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['Title'], 'Earlier HTTP result')
        self.assertEqual(rows[0]['DOI'], DOI)
        job = json.loads((self.folder / 'results.audit.json').read_text())['jobs'][0]
        self.assertEqual(job['status'], 'blocked')
        self.assertEqual(job['retrieved'], 1)
        self.assertEqual([(a['mode'], a['status'], a['retrieved']) for a in job['attempts']],
                         [('http', 'blocked', 1), ('browser', 'blocked', 0)])
        self.assertIn('verification', job['attempts'][0]['error'])
        self.assertIn('timed out', job['attempts'][1]['error'])
        self.assertIs(browser.call_args.args[0], page)
        context.close.assert_called_once()
        playwright.stop.assert_called_once()

    def test_year_filter_preserves_unknown_year_and_audits_raw_count(self):
        self.adapter('search_tr_dizin', return_value=self.result([
            {'Title': 'Old', 'Year': '2001', 'Link': 'https://tr.example/1'},
            {'Title': 'Recent', 'Year': '2023', 'Link': 'https://tr.example/2'},
            {'Title': 'Unknown', 'Year': '', 'Link': 'https://tr.example/3'}]))
        self.assertEqual(turkish.main(['karst', '--year-from', '2020'] + self.outargs('tr_dizin')), 0)
        self.assertEqual({row['Title'] for row in self.read_csv(self.folder / 'results.csv')}, {'Recent', 'Unknown'})
        audit = json.loads((self.folder / 'results.audit.json').read_text())
        self.assertEqual(audit['retrieved_before_year_filter'], 3)
        self.assertEqual(audit['exported'], 2)

    def test_noargs_can_prompt_for_keyword(self):
        self.adapter('search_tr_dizin', return_value=self.result([]))
        self.stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=True))
        entered = self.stack.enter_context(patch('builtins.input', return_value='yeraltı suyu'))
        self.assertEqual(turkish.main(self.outargs('tr_dizin')), 0)
        entered.assert_called_once()
        audit = json.loads((self.folder / 'results.audit.json').read_text())
        self.assertEqual(audit['keywords'], ['yeraltı suyu'])

    def test_existing_output_is_protected_before_search(self):
        path = self.folder / 'results.csv'
        path.write_text('original')
        adapter = self.adapter('search_tr_dizin', side_effect=AssertionError('Search should not start'))
        with self.assertRaises(SystemExit) as raised:
            turkish.main(['karst'] + self.outargs('tr_dizin'))
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(path.read_text(), 'original')
        adapter.assert_not_called()

    def test_explicit_overwrite_replaces_old_csv_and_audit(self):
        path = self.folder / 'results.csv'
        path.write_text('original')
        path.with_suffix('.audit.json').write_text('old audit')
        self.adapter('search_tr_dizin', return_value=self.result([{'Title': 'New result', 'DOI': DOI}]))
        self.assertEqual(turkish.main(['karst', '--overwrite'] + self.outargs('tr_dizin')), 0)
        self.assertEqual(self.read_csv(path)[0]['Title'], 'New result')
        self.assertEqual(json.loads(path.with_suffix('.audit.json').read_text())['exported'], 1)


if __name__ == '__main__':
    unittest.main()
