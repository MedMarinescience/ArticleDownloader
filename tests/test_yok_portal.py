import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from turkish_literature import search_yok_tez, yok_parse_results, yok_reference_data


HOME = '''<form method="post" action="SearchTez"><input name="neden">
<input type="hidden" name="izin" value="0"><input type="hidden" name="tur" value="0">
<input type="hidden" name="islem" value="1"></form>'''
RESULT = '''<div id="results-body"><div class="result-card" data-index="0"
data-kayitno="abc-1" data-tezno="def_2"><div class="card-title">Kıyı suyu</div>
<strong>Tez No:</strong> 123456</div></div><div class="result-count-text">
Arama sonucunda 1 kayıt bulundu.</div><script>const referenceData = {
"0": {"meta": {"author": "ÇAĞRI ŞAHİN", "year":"2025", "lang":"Türkçe",
"yer":"Üniversite", "title":"Kıyı suyu", "subject":"Jeoloji", "type":"Doktora"}},
};var opts={url:"tezBilgiDetay.jsp"};</script>'''


class Response:
    def __init__(self, text, status=200, url="https://tez.yok.gov.tr/UlusalTezMerkezi/"):
        self.text, self.status_code, self.url = text, status, url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    def __init__(self, home=HOME, result=RESULT, status=200):
        self.home, self.result, self.status, self.calls = home, result, status, []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return Response(self.home)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response(self.result, self.status, url)


class YokTests(unittest.TestCase):
    def test_live_form_contract_session_headers_and_field_mapping(self):
        session = Session()
        result = search_yok_tez(session, "kıyı suyu", max_results=5)
        self.assertEqual(result["status"], "completed")
        row = result["rows"][0]
        self.assertEqual(row["Title"], "Kıyı suyu")
        self.assertEqual(row["Authors"], "ÇAĞRI ŞAHİN")
        self.assertEqual(row["Thesis_ID"], "123456")
        self.assertIn("kayitNo=abc-1&tezNo=def_2", row["Link"])
        self.assertEqual(session.calls[1][2]["data"], {"izin":"0", "tur":"0", "islem":"1", "neden":"kıyı suyu", "nevi":"7"})
        self.assertIn("Referer", session.calls[1][2]["headers"])

    def test_safe_trailing_comma_parse_preserves_string_punctuation(self):
        obj = yok_reference_data('const referenceData={"0":{"meta":{"title":"comma, } and \\\"quote\\\""}},};')
        self.assertEqual(obj["0"]["meta"]["title"], 'comma, } and "quote"')

    def test_unrecognized_response_is_failed_not_empty_success(self):
        result = search_yok_tez(Session(result="<html>Maintenance</html>"), "suyu")
        self.assertEqual(result["status"], "failed")
        self.assertIn("not recognized", result["error"])

    def test_block_response_reported_not_parsed_as_empty(self):
        result = search_yok_tez(Session(result="Blocked", status=418), "suyu")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["rows"], [])

    def test_empty_results_require_actual_counter(self):
        result = search_yok_tez(Session(result='<div class="result-count-text">Arama sonucunda 0 kayıt bulundu.</div>'), "suyu")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_reported"], 0)

    def test_live_zero_result_marker(self):
        result = search_yok_tez(Session(result="<h2>Sonuç bulunamadı</h2>"), "suyu")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_reported"], 0)

    def test_embedded_pagination_limit_is_capped(self):
        html = RESULT.replace("1 kayıt", "1.027 kayıt")
        result = search_yok_tez(Session(result=html), "suyu", max_results=1)
        self.assertEqual(result["status"], "capped")
        self.assertEqual(result["total_reported"], 1027)


if __name__ == "__main__":
    unittest.main()
