"""Offline regressions: synthetic PDFs and mocked HTTP, never real articles."""
import contextlib
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scihub_downloader as core
import search_articles7 as search
import selected_pdf_integration as wrapper
import turkish_literature as turkish
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

DOI = "10.1234/test.article"
MIRROR = "https://mirror.example"


def make_pdf(doi=DOI):
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    # ASCII DOI fixture has no PDF string escaping requirements.
    stream.set_data(f"BT /F1 12 Tf 36 700 Td ({doi}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


PDF = make_pdf()


class Response:
    def __init__(self, url, body=b"", status=200, headers=None):
        self.url, self.body, self.status_code = url, body, status
        self.headers = headers or {}
        self.encoding = "utf-8"
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


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.calls = []
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def downloader(self, **kwargs):
        downloader = core.SciHubDownloader([MIRROR], self.folder / "pdfs", delay=0, **kwargs)
        self.addCleanup(downloader.close)
        return downloader

    def http(self, responder):
        def request(method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return responder(method, url, kwargs)
        mock = patch("requests.Session.request", side_effect=request)
        mock.start()
        self.addCleanup(mock.stop)

    def selected(self, rows=None):
        path = self.folder / "selected.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, ["Title", "DOI", "Notes"], delimiter=";")
            writer.writeheader()
            writer.writerows(rows or [{"Title": "Göksu kıyısı", "DOI": DOI, "Notes": "keep me"}])
        return path

    def search_args(self, selected, provider="scihub"):
        return ["search_articles7.py", "--from-csv", str(selected), "--download-pdfs",
                "--pdf-provider", provider, "--mirror", MIRROR, "--scihub-delay", "0",
                "--pdf-dir", str(self.folder / "pdfs"), "--out", str(self.folder / "result.csv")]

    def test_direct_pdf_get_encodes_identifier_and_checks_actual_doi(self):
        doi = "10.1234/name(1)?x#part"
        body = make_pdf(doi)
        self.http(lambda method, url, kw: Response(url, body))
        result = self.downloader().download("https://doi.org/" + doi)
        self.assertEqual(self.calls[0][1], MIRROR + "/" + quote(doi, safe="/"))
        self.assertEqual(result["PDF_status"], "downloaded")
        self.assertEqual(result["PDF_DOI_check"], "doi_seen_in_first_three_pages")
        self.assertEqual(Path(result["Pdf_path"]).read_bytes(), body)

    def test_pdf_layouts_relative_and_protocol_relative_links(self):
        layouts = [('<iframe id="pdf" src="/paper.pdf#view=FitH"></iframe>', MIRROR + "/paper.pdf"),
                   ("<iframe id src='/paper.pdf'></iframe>", MIRROR + "/paper.pdf"),
                   ('<embed src="//cdn.example/paper.pdf">', "https://cdn.example/paper.pdf"),
                   ('<object data="/paper.pdf"></object>', MIRROR + "/paper.pdf"),
                   ('<button onclick="location.href=\'/paper.pdf\'">save</button>', MIRROR + "/paper.pdf"),
                   ('<meta name="citation_pdf_url" content="/paper.pdf">', MIRROR + "/paper.pdf"),
                   ('<a class="download" href="/download?id=4">save</a>', MIRROR + "/download?id=4"),
                   ('<div class="download"><span><a href="/download?id=5">save</a></span></div>', MIRROR + "/download?id=5"),
                   ('<a href="/file?id=6"><span>Download PDF</span></a>', MIRROR + "/file?id=6"),
                   ('<a href="/downloads?id=7">Get document</a>', MIRROR + "/downloads?id=7")]
        for number, (html, expected) in enumerate(layouts):
            with self.subTest(html=html):
                self.calls.clear()
                self.http(lambda method, url, kw, html=html, expected=expected:
                          Response(url, PDF if url == expected else html.encode()))
                downloader = core.SciHubDownloader([MIRROR], self.folder / str(number), delay=0)
                with downloader:
                    result = downloader.download(DOI)
                self.assertEqual(result["PDF_status"], "downloaded")
                self.assertEqual(self.calls[-1][1], expected)
                self.assertEqual(self.calls[-1][2]["headers"]["Referer"], MIRROR + "/" + DOI)

    def test_post_fallback_uses_form_action_and_request_body(self):
        def response(method, url, kw):
            if method == "POST":
                self.assertEqual(url, MIRROR + "/lookup")
                self.assertEqual(kw["data"]["request"], DOI)
                return Response(url, b'<iframe src="/paper.pdf"></iframe>')
            return Response(url, PDF if url.endswith("paper.pdf") else b'<form method="post" action="/lookup"></form>')
        self.http(response)
        result = self.downloader().download(DOI)
        self.assertEqual(result["PDF_status"], "downloaded")
        self.assertEqual([item[0] for item in self.calls], ["GET", "POST", "GET"])

    def test_homepage_form_discovery_when_doi_get_is_not_supported(self):
        def response(method, url, kw):
            if method == "POST":
                self.assertEqual(url, MIRROR + "/search")
                self.assertEqual(kw["data"]["request"], DOI)
                return Response(url, b'<object data="/paper.pdf"></object>')
            if url == MIRROR + "/":
                return Response(url, b'<form method="post" action="/search"></form>')
            return Response(url, PDF if url.endswith("paper.pdf") else b"", status=200 if url.endswith("paper.pdf") else 404)
        self.http(response)
        result = self.downloader().download(DOI)
        self.assertEqual(result["PDF_status"], "downloaded")
        self.assertIn(("GET", MIRROR + "/"), [(method, url) for method, url, kw in self.calls])

    def test_mirror_failover_after_unavailable_article(self):
        self.http(lambda method, url, kw: Response(url, PDF if "second.example" in url else b"Article not found"))
        with core.SciHubDownloader([MIRROR, "https://second.example"], self.folder, delay=0) as downloader:
            result = downloader.download(DOI)
        self.assertEqual(result["PDF_status"], "downloaded")
        self.assertIn("article unavailable", result["PDF_error"])

    def test_retry_after_closes_response_and_retries(self):
        retry = Response(MIRROR, status=429, headers={"Retry-After": "0"})
        self.http(lambda method, url, kw: retry if len(self.calls) == 1 else Response(url, PDF))
        with patch.object(core.time, "sleep"):
            result = self.downloader().download(DOI)
        self.assertEqual(result["PDF_status"], "downloaded")
        self.assertTrue(retry.closed)
        self.assertEqual(len(self.calls), 2)

    def test_challenge_skips_post_and_reports_failure(self):
        self.http(lambda method, url, kw: Response(url, b"Please verify you are human CAPTCHA"))
        result = self.downloader().download(DOI)
        self.assertEqual(result["PDF_status"], "not_downloaded")
        self.assertIn("challenge page", result["PDF_error"])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(list((self.folder / "pdfs").glob("*.pdf")))

    def test_challenge_iframe_is_not_fetched_as_an_article(self):
        challenge = b'<div class="hcaptcha"><iframe src="https://captcha.example/widget"></iframe></div>'
        self.http(lambda method, url, kw: Response(url, challenge))
        result = self.downloader().download(DOI)
        self.assertEqual(result["PDF_status"], "not_downloaded")
        self.assertIn("challenge page", result["PDF_error"])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("captcha.example", self.calls[0][1])

    def test_dormant_captcha_and_cloudflare_cdn_scripts_do_not_block_pdf(self):
        html = (b'<script src="https://cdnjs.cloudflare.com/a.js"></script>'
                b'<script src="/captcha.js"></script><iframe src="/paper.pdf"></iframe>')
        self.http(lambda method, url, kw: Response(url, PDF if url.endswith("paper.pdf") else html))
        result = self.downloader().download(DOI)
        self.assertEqual(result["PDF_status"], "downloaded")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[-1][1].endswith("/paper.pdf"))

    def test_html_cannot_be_saved_as_pdf_even_with_pdf_content_type(self):
        self.http(lambda method, url, kw: Response(url, b"<html>Sign in</html>", headers={"Content-Type": "application/pdf"}))
        downloader = self.downloader()
        with self.assertRaisesRegex(ValueError, "not a PDF"):
            downloader.save(MIRROR + "/fake.pdf", downloader.folder / "fake.pdf", DOI)
        self.assertEqual(list(downloader.folder.iterdir()), [])

    def test_malformed_pdf_is_rejected_and_partial_removed(self):
        self.http(lambda method, url, kw: Response(url, b"%PDF-1.7\nthis is corrupt"))
        downloader = self.downloader()
        with self.assertRaises(Exception):
            downloader.save(MIRROR + "/fake.pdf", downloader.folder / "fake.pdf", DOI)
        self.assertEqual(list(downloader.folder.iterdir()), [])

    def test_size_limit_removes_partial_and_does_not_publish(self):
        self.http(lambda method, url, kw: Response(url, PDF))
        downloader = self.downloader(max_mb=0.0001)
        with self.assertRaisesRegex(ValueError, "size limit"):
            downloader.save(MIRROR + "/paper.pdf", downloader.folder / "paper.pdf", DOI)
        self.assertEqual(list(downloader.folder.iterdir()), [])

    def test_existing_pdf_reused_without_network(self):
        downloader = self.downloader()
        path = downloader.folder / core.safe_pdf_filename(DOI)
        path.write_bytes(PDF)
        self.http(lambda *args: self.fail("An existing valid PDF needs no request"))
        result = downloader.download(DOI)
        self.assertEqual(result["PDF_status"], "already_present")
        self.assertEqual(result["PDF_provider"], "Local cache")
        self.assertFalse(result.get("PDF_URL"))

    def test_reused_pdf_preserves_only_checksum_and_path_matched_provenance(self):
        path = self.folder / core.safe_pdf_filename(DOI)
        path.write_bytes(PDF)
        digest = core.sha256_file(path)
        rows = [{"DOI": DOI, "Pdf_path": str(path), "PDF_SHA256": digest,
                 "PDF_provider": "Unpaywall", "PDF_URL": "https://repository.example/original.pdf"},
                {"DOI": DOI, "Pdf_path": str(path), "PDF_SHA256": "wrong-checksum",
                 "PDF_provider": "Unpaywall", "PDF_URL": "https://repository.example/wrong.pdf"},
                {"DOI": DOI, "Pdf_path": str(self.folder / "different.pdf"), "PDF_SHA256": digest,
                 "PDF_provider": "Unpaywall", "PDF_URL": "https://repository.example/different.pdf"}]
        self.http(lambda *args: self.fail("An existing valid PDF needs no request"))
        self.assertEqual(core.download_rows(rows, self.folder, [MIRROR], delay=0), 0)
        self.assertEqual(rows[0]["PDF_provider"], "Unpaywall")
        self.assertEqual(rows[0]["PDF_URL"], "https://repository.example/original.pdf")
        for row in rows[1:]:
            self.assertEqual(row["PDF_provider"], "Local cache")
            self.assertEqual(row["PDF_URL"], "")

    def test_corrupt_existing_pdf_quarantined_before_retry(self):
        downloader = self.downloader()
        path = downloader.folder / core.safe_pdf_filename(DOI)
        path.write_bytes(b"<html>old broken download</html>")
        self.http(lambda method, url, kw: Response(url, PDF))
        result = downloader.download(DOI)
        self.assertEqual(result["PDF_status"], "downloaded")
        self.assertEqual(path.read_bytes(), PDF)
        self.assertEqual(len(list(downloader.folder.glob("*.invalid-*"))), 1)

    def test_duplicates_cached_invalid_doi_reported_and_notes_preserved(self):
        self.http(lambda method, url, kw: Response(url, PDF))
        rows = [{"DOI": DOI, "Notes": "a"}, {"DOI": "doi:" + DOI, "Notes": "b"}, {"DOI": ""}]
        failed = core.download_rows(rows, self.folder, [MIRROR], delay=0)
        self.assertEqual(failed, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([row.get("Notes") for row in rows], ["a", "b", None])
        self.assertEqual(rows[-1]["PDF_status"], "missing_or_invalid_doi")

    def test_unexpected_per_article_error_does_not_stop_later_downloads(self):
        self.http(lambda method, url, kw: Response(url, PDF))
        original = core.SciHubDownloader.download
        def download(downloader, doi):
            if doi == "10.1234/broken":
                raise OSError("cannot quarantine this file")
            return original(downloader, doi)
        rows = [{"DOI": "10.1234/broken"}, {"DOI": DOI}]
        with patch.object(core.SciHubDownloader, "download", new=download):
            failed = core.download_rows(rows, self.folder, [MIRROR], delay=0)
        self.assertEqual(failed, 1)
        self.assertEqual(rows[0]["PDF_status"], "failed")
        self.assertIn("cannot quarantine", rows[0]["PDF_error"])
        self.assertEqual(rows[1]["PDF_status"], "downloaded")
        self.assertTrue(Path(rows[1]["Pdf_path"]).is_file())

    def test_semicolon_csv_and_count_limit(self):
        selected = self.selected([{"Title": "Türkçe", "DOI": DOI, "Notes": "one"},
                                  {"Title": "Other", "DOI": "10.1234/other", "Notes": "two"}])
        self.http(lambda method, url, kw: Response(url, PDF))
        report = self.folder / "report.csv"
        code = core.main(["download", "--file", str(selected), "--source", "csv", "--count", "1",
                          "--mirror", MIRROR, "--scihub-delay", "0", "--output-dir", str(self.folder / "pdfs"),
                          "--report", str(report)])
        self.assertEqual(code, 0)
        with report.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Title"], "Türkçe")

    def test_main_selected_csv_preserves_rows_extra_fields_and_audit(self):
        selected = self.selected([{"Title": "Göksu kıyısı", "DOI": DOI, "Notes": "first"},
                                  {"Title": "Göksu kıyısı", "DOI": DOI, "Notes": "second"}])
        original = selected.read_bytes()
        self.http(lambda method, url, kw: Response(url, PDF))
        with patch.object(sys, "argv", self.search_args(selected)):
            code = search.main()
        self.assertEqual(code, 0)
        self.assertEqual(selected.read_bytes(), original)
        with (self.folder / "result.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([r["Notes"] for r in rows], ["first", "second"])
        self.assertTrue(all(r["PDF_provider"] == "SciHub" for r in rows))
        audit = json.loads((self.folder / "result.audit.json").read_text())
        self.assertEqual(audit["pdf_download"]["unavailable_rows"], 0)
        self.assertEqual(len(self.calls), 1)

    def test_main_reports_unavailable_rows_with_nonzero_exit(self):
        selected = self.selected()
        self.http(lambda method, url, kw: Response(url, b"Article not found"))
        with patch.object(sys, "argv", self.search_args(selected)):
            code = search.main()
        self.assertEqual(code, 1)
        with (self.folder / "result.csv").open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["PDF_status"], "not_downloaded")
        self.assertEqual(row["Pdf_path"], "")
        self.assertIn("article unavailable", row["PDF_error"])

    def test_turkish_import_downloads_doi_and_keeps_unicode_provenance(self):
        selected = self.folder / "turkish.csv"
        selected.write_text("Makale Adı;DOI;Yazar\nGöksu kıyısı;" + DOI + ";Çağrı Şahin\n", encoding="utf-8-sig")
        output = self.folder / "normalized.csv"
        self.http(lambda method, url, kw: Response(url, PDF))
        argv = ["turkish_literature.py", "import", "--source", "dergipark", "--input", str(selected),
                "--out", str(output), "--delimiter", ";", "--download-pdfs", "--mirror", MIRROR,
                "--scihub-delay", "0", "--pdf-dir", str(self.folder / "pdfs")]
        with patch.object(sys, "argv", argv):
            code = turkish.main()
        self.assertEqual(code, 0)
        with output.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["Title"], "Göksu kıyısı")
        self.assertEqual(row["Authors"], "Çağrı Şahin")
        self.assertEqual(row["Source"], "DergiPark")
        self.assertEqual(row["PDF_status"], "downloaded")
        self.assertTrue(Path(str(output) + ".import.jsonl").is_file())

    def test_turkish_metadata_only_import_needs_no_network(self):
        selected = self.folder / "turkish.csv"
        selected.write_text("Makale Adı;DOI\nGöksu kıyısı;" + DOI + "\n", encoding="utf-8-sig")
        output = self.folder / "normalized.csv"
        self.http(lambda *args: self.fail("Metadata import should stay offline"))
        argv = ["turkish_literature.py", "import", "--source", "dergipark", "--input", str(selected),
                "--out", str(output), "--delimiter", ";"]
        with patch.object(sys, "argv", argv):
            self.assertEqual(turkish.main(), 0)
        self.assertTrue(output.is_file())

    def test_wrapper_uses_integrated_main_and_restores_process_state(self):
        selected = self.selected()
        self.http(lambda method, url, kw: Response(url, PDF))
        old_args, old_path = sys.argv[:], sys.path[:]
        code = wrapper.main(["--from-csv", str(selected), "--mirror", MIRROR, "--delay", "0",
                             "--pdf-dir", str(self.folder / "pdfs"), "--out", str(self.folder / "wrapped.csv")])
        self.assertEqual(code, 0)
        self.assertEqual(sys.argv, old_args)
        self.assertEqual(sys.path, old_path)
        self.assertTrue((self.folder / "wrapped.csv").is_file())

    def test_both_provider_does_not_hit_scihub_when_oa_succeeds(self):
        selected = self.selected()
        self.http(lambda method, url, kw: Response(url, PDF) if "repository.example" in url else self.fail("Unexpected Sci-Hub lookup"))
        with patch.dict("os.environ", {"UNPAYWALL_EMAIL": "test@example.org"}), \
             patch.object(search, "lookup_unpaywall_pdf", return_value="https://repository.example/paper.pdf"), \
             patch.object(sys, "argv", self.search_args(selected, "both")):
            code = search.main()
        self.assertEqual(code, 0)
        with (self.folder / "result.csv").open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["PDF_provider"], "Unpaywall")
        self.assertEqual(len(self.calls), 1)

    def test_both_provider_falls_back_to_scihub_when_oa_missing(self):
        selected = self.selected()
        self.http(lambda method, url, kw: Response(url, PDF))
        with patch.dict("os.environ", {"UNPAYWALL_EMAIL": "test@example.org"}), \
             patch.object(search, "lookup_unpaywall_pdf", return_value=""), \
             patch.object(sys, "argv", self.search_args(selected, "both")):
            code = search.main()
        self.assertEqual(code, 0)
        with (self.folder / "result.csv").open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["PDF_provider"], "SciHub")
        self.assertIn("[Unpaywall]", row["PDF_error"])
        self.assertIn("Unpaywall returned no PDF location", row["PDF_error"])


if __name__ == "__main__":
    unittest.main()
