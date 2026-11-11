#!/usr/bin/env python3
"""Search YÖK Tez, TR Dizin and DergiPark directly by keyword; export UTF-8 CSV."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlencode, unquote

import requests
from bs4 import BeautifulSoup

PORTALS = {
    'yok_tez': ('YOKTez', 'https://tez.yok.gov.tr/UlusalTezMerkezi/'),
    'tr_dizin': ('TRDizin', 'https://search.trdizin.gov.tr/'),
    'dergipark': ('DergiPark', 'https://dergipark.org.tr/'),
}
CSV_FIELDS = ['Title', 'Authors', 'Year', 'DOI', 'Link', 'Source', 'Query',
              'Journal', 'Abstract', 'Document_type', 'Thesis_ID', 'Record_IDs', 'Links']
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


class SearchSession(requests.Session):
    """Session cookies, per-request pacing, bounded retry and an audit trail."""
    def __init__(self, delay=1, timeout=30):
        super().__init__()
        self.headers.update({'User-Agent': BROWSER_UA,
                             'Accept-Language': 'tr-TR,tr;q=0.9,en;q=0.7'})
        self.delay, self.timeout = delay, timeout
        self.last_request = None
        self.request_log = []

    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        for attempt in range(3):
            if self.last_request is not None:
                wait = self.delay - (time.monotonic() - self.last_request)
                if wait > 0:
                    time.sleep(wait)
            self.last_request = time.monotonic()
            entry = {'method': method.upper(), 'url': url, 'attempt': attempt + 1}
            try:
                response = super().request(method, url, **kwargs)
            except requests.RequestException as exc:
                entry['error'] = str(exc)
                self.request_log.append(entry)
                raise
            entry.update(status=response.status_code, final_url=response.url)
            self.request_log.append(entry)
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                return response
            retry_after = response.headers.get('Retry-After', '')
            response.close()
            try:
                wait = float(retry_after)
            except ValueError:
                wait = 2 ** attempt
            time.sleep(min(max(wait, 0), 30) if math.isfinite(wait) else 2 ** attempt)
        raise RuntimeError('Search request retry limit reached')




"""YÖK Tez adapter: native public search form; no export file needed."""
import json
import re
from urllib.parse import urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


YOK_BASE = "https://tez.yok.gov.tr/UlusalTezMerkezi/"
YOK_BROWSER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/130.0.0.0 Safari/537.36")


def yok_compact(value):
    return " ".join(str(value or "").split())


def yok_block_reason(response):
    if response.status_code in (401, 403, 418, 429):
        return f"YÖK Tez access blocked or rate limited (HTTP {response.status_code})"
    if re.search(r"your access to this page has been blocked|verify you are human|"
                 r"cf-chl-|<title[^>]*>[^<]*(?:captcha|access denied)", response.text, re.I):
        return "YÖK Tez returned an access challenge rather than search results"
    return ""


def yok_reference_data(text):
    """Parse the portal's data object, removing JS trailing commas without eval."""
    start = re.search(r"\b(?:const|let|var)\s+referenceData\s*=\s*\{", text)
    if not start:
        return {}
    pos = start.end() - 1
    depth, quoted, escaped, end = 0, False, False, None
    for index in range(pos, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        return {}
    raw = text[pos:end]
    cleaned = []
    quoted = escaped = False
    for index, char in enumerate(raw):
        if quoted:
            cleaned.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            cleaned.append(char)
        elif char == "," and raw[index + 1:].lstrip().startswith(("}", "]")):
            continue
        else:
            cleaned.append(char)
    try:
        result = json.loads("".join(cleaned))
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def yok_parse_results(html, keyword, response_url, max_results):
    soup = BeautifulSoup(html, "html.parser")
    count_node = soup.select_one(".result-count-text")
    count_text = yok_compact(count_node.get_text(" ", strip=True)) if count_node else ""
    count_match = re.search(r"([\d.,]+)\s+kay[ıi]t\s+bulundu", count_text, re.I)
    total = int(re.sub(r"\D", "", count_match.group(1))) if count_match else None
    cards = soup.select("#results-body .result-card")
    if not cards:
        if total == 0 or re.search(r"(?:arama sonucunda\s+)?0\s+kay[ıi]t\s+bulundu|"
                                    r"araman[ıi]za uygun (?:bir )?(?:tez|kay[ıi]t) bulunamad[ıi]|"
                                    r"sonuç\s+bulunamad[ıi]", soup.get_text(" "), re.I):
            return [], 0, ""
        return [], total, "YÖK Tez response layout was not recognized; no confirmed empty-result marker"
    references = yok_reference_data(html)
    detail_route = re.search(r"url\s*:\s*['\"]([^'\"]*tezBilgiDetay\.jsp)['\"]", html)
    rows, seen = [], set()
    for card in cards:
        if len(rows) >= max_results:
            break
        title_node = card.select_one(".card-title")
        if title_node is None:
            continue
        title = yok_compact(title_node.get_text(" ", strip=True))
        if not title:
            continue
        number_match = re.search(r"Tez\s+No\s*:\s*(\d+)", card.get_text(" ", strip=True), re.I)
        number = number_match.group(1) if number_match else ""
        record_key = number or card.get("data-kayitno") or title
        if record_key in seen:
            continue
        seen.add(record_key)
        data = references.get(str(card.get("data-index", "")), {})
        meta = data.get("meta", {}) if isinstance(data, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        record_link = response_url
        if detail_route and card.get("data-kayitno") and card.get("data-tezno"):
            record_link = urljoin(response_url, detail_route.group(1)) + "?" + urlencode({
                "kayitNo": card["data-kayitno"], "tezNo": card["data-tezno"]})
        rows.append({"Title": title, "Authors": yok_compact(meta.get("author")),
                     "Year": yok_compact(meta.get("year")), "DOI": "", "Link": record_link,
                     "Source": "YOKTez", "Query": keyword, "Thesis_ID": number,
                     "Abstract": "", "Journal": "", "Document_type": "thesis",
                     "Institution": yok_compact(meta.get("yer")),
                     "Language": yok_compact(meta.get("lang")),
                     "Keywords": yok_compact(meta.get("subject")),
                     "Thesis_type": yok_compact(meta.get("type"))})
    if not rows:
        return [], total, "YÖK Tez result cards lacked readable titles"
    # The current portal embeds every result card in one response and paginates
    # only by hiding/showing cards in JavaScript. No page requests are invented.
    return rows, total if total is not None else len(cards), ""


def search_yok_tez(session, keyword, max_results=50, max_pages=5, timeout=30):
    report = {"rows": [], "status": "failed", "error": "", "search_url": YOK_BASE,
              "pages_fetched": 0, "total_reported": None}
    if len(keyword.strip()) < 3:
        report["error"] = "YÖK Tez requires a keyword of at least three characters"
        return report
    if max_results < 1 or max_pages < 1:
        report["error"] = "Result and page limits must be positive"
        return report
    headers = {"User-Agent": YOK_BROWSER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7"}
    try:
        home = session.get(YOK_BASE, timeout=timeout, headers=headers)
        reason = yok_block_reason(home)
        if reason:
            report.update(status="blocked", error=reason)
            return report
        home.raise_for_status()
        soup = BeautifulSoup(home.text, "html.parser")
        form = next((node for node in soup.find_all("form") if node.find("input", attrs={"name": "neden"})), None)
        if form is None:
            report["error"] = "YÖK Tez search form changed: input named 'neden' was not found"
            return report
        action = urljoin(home.url, form.get("action") or home.url)
        if urlsplit(action).netloc != urlsplit(YOK_BASE).netloc:
            report["error"] = "YÖK Tez search form points outside the portal"
            return report
        report["search_url"] = action
        data = {node["name"]: node.get("value", "") for node in form.select('input[type="hidden"][name]')}
        # Homepage submitSearch() injects nevi=7 when no field checkbox is selected
        # (all catalogue fields), as observed in the live page source.
        data.update(neden=keyword.strip(), nevi="7")
        headers.update(Referer=home.url, Origin="https://" + urlsplit(home.url).netloc)
        if form.get("method", "get").lower() == "post":
            response = session.post(action, data=data, timeout=timeout, headers=headers)
        else:
            response = session.get(action, params=data, timeout=timeout, headers=headers)
        report["pages_fetched"] = 1
        reason = yok_block_reason(response)
        if reason:
            report.update(status="blocked", error=reason)
            return report
        response.raise_for_status()
        rows, total, error = yok_parse_results(response.text, keyword, response.url, max_results)
        report.update(rows=rows, total_reported=total, error=error)
        if error:
            return report
        report["status"] = "capped" if total is not None and total > len(rows) else "completed"
        return report
    except requests.RequestException as exc:
        report["error"] = f"YÖK Tez request failed: {type(exc).__name__}: {exc}"
        return report


"""Live TR Dizin public search adapter; endpoints verified from its Angular client.

The site's assets/env.json overrides its bundled development/management URL.
Search is the native portal query (it can include both papers and projects), not
local substring matching. Current public pagination accepts limit=20.
"""
import re
from html import unescape
from urllib.parse import quote, urlencode, urlsplit


def trdizin_text(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]*>', ' ', str(value)))).strip()


def trdizin_doi(value):
    text = trdizin_text(value)
    text = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', text, flags=re.I).strip()
    return text.lower() if re.fullmatch(r'10\.\d{4,9}/\S+', text, re.I) else ''


def trdizin_order(value):
    try:
        return float(value.get('order', 0) or 0)
    except (TypeError, ValueError):
        return 0


def trdizin_row(hit, keyword):
    source = hit.get('_source')
    if not isinstance(source, dict):
        raise ValueError('TR Dizin search hit has no _source object')
    ident = source.get('id') or hit.get('_id')
    abstracts = source.get('abstracts') or []
    if not isinstance(abstracts, list):
        raise ValueError('TR Dizin abstracts have an unexpected schema')
    abstracts = [item for item in abstracts if isinstance(item, dict)]
    lang = source.get('language') or 'TUR'
    selected = next((a for a in abstracts if a.get('language') == lang and a.get('title')), None)
    selected = selected or next((a for a in abstracts if a.get('language') == 'TUR' and a.get('title')), None)
    selected = selected or next((a for a in abstracts if a.get('title')), {})
    title = trdizin_text(selected.get('title'))
    if not title or not ident:
        raise ValueError('TR Dizin search hit lacks a title or record identifier')
    authors = source.get('authors') or []
    if not isinstance(authors, list):
        raise ValueError('TR Dizin authors have an unexpected schema')
    authors = sorted((a for a in authors if isinstance(a, dict)), key=trdizin_order)
    journal = source.get('journal') or {}
    issue = source.get('issue') or {}
    if not isinstance(journal, dict) or not isinstance(issue, dict):
        raise ValueError('TR Dizin journal/issue have an unexpected schema')
    keywords = selected.get('keywords') or []
    if isinstance(keywords, list):
        keywords = '; '.join(trdizin_text(k.get('name') or k.get('keyword')) if isinstance(k, dict) else trdizin_text(k) for k in keywords)
    doc_type = {'PAPER': 'article', 'PROJECT': 'project'}.get(source.get('docType'), trdizin_text(source.get('docType')).lower() or 'publication')
    return {
        'Title': title,
        'Authors': '; '.join(trdizin_text(a.get('inPublicationName') or a.get('name')) for a in authors if a.get('name') or a.get('inPublicationName')),
        'Year': trdizin_text(source.get('publicationYear') or issue.get('year')),
        'DOI': trdizin_doi(source.get('doi')),
        'Link': 'https://search.trdizin.gov.tr/tr/yayin/detay/' + quote(str(ident), safe=''),
        'Source': 'TRDizin', 'Query': keyword,
        'Abstract': trdizin_text(selected.get('abstract')),
        'Journal': trdizin_text(journal.get('name') or journal.get('title')),
        'Document_type': doc_type, 'Record_IDs': 'TRDizin:' + str(ident),
        'Language': trdizin_text(selected.get('language') or source.get('language')),
        'Keywords': trdizin_text(keywords),
        'Volume': trdizin_text(issue.get('volume')), 'Issue': trdizin_text(issue.get('number')),
        'Pages': '-'.join(str(x) for x in (source.get('startPage'), source.get('endPage')) if x is not None and str(x)),
    }


def trdizin_get_json(session, url, timeout, **kwargs):
    with session.get(url, timeout=timeout, **kwargs) as response:
        if response.status_code != 200:
            error = RuntimeError('TR Dizin HTTP ' + str(response.status_code) + ' at ' + response.url)
            error.blocked = response.status_code in (401, 403, 429)
            raise error
        try:
            value = response.json()
        except (ValueError, TypeError) as exc:
            text = response.text[:10000]
            error = RuntimeError('TR Dizin returned a non-JSON page; search was not completed')
            error.blocked = bool(re.search(r'captcha|cf-chl-|access denied|verify.*human', text, re.I))
            raise error from exc
        if not isinstance(value, dict):
            raise ValueError('TR Dizin returned an unexpected JSON root')
        return value


def search_tr_dizin(session, keyword, max_results=50, max_pages=5, timeout=30):
    """Execute a live keyword query and return rows plus explicit coverage status."""
    search_url = 'https://search.trdizin.gov.tr/tr/yayin/ara?' + urlencode({'q': keyword})
    result = {'rows': [], 'status': 'failed', 'error': '', 'search_url': search_url,
              'pages_fetched': 0, 'total_reported': None}
    seen = set()
    try:
        if not keyword.strip() or max_results < 1 or max_pages < 1:
            raise ValueError('Keyword must be nonempty and result/page limits positive')
        config = trdizin_get_json(session, 'https://search.trdizin.gov.tr/assets/env.json', timeout)
        api_base = str(config.get('apiUrl') or '').rstrip('/')
        parts = urlsplit(api_base)
        if parts.scheme != 'https' or parts.hostname != 'search.trdizin.gov.tr' or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('TR Dizin runtime apiUrl changed; inspect the portal before updating the adapter')
        api_url = api_base + '/api/defaultSearch/publication/'
        scanned = 0
        # 5 is silently normalized to 10 by the current service; 20 is a verified
        # portal-supported size, and stable across pages. Trim only the output.
        page_size = 20
        for page in range(1, max_pages + 1):
            data = trdizin_get_json(session, api_url, timeout, params={
                'q': keyword, 'order': 'relevance-DESC', 'page': page, 'limit': page_size})
            if data.get('error') or data.get('timed_out') or (data.get('_shards') or {}).get('failed', 0):
                raise ValueError('TR Dizin reported a query error, timeout or incomplete shard response')
            hits_root = data.get('hits')
            if not isinstance(hits_root, dict) or not isinstance(hits_root.get('hits'), list):
                raise ValueError('TR Dizin search response no longer has hits.hits')
            total_info = hits_root.get('total')
            total = total_info.get('value') if isinstance(total_info, dict) else total_info
            relation = total_info.get('relation', 'eq') if isinstance(total_info, dict) else 'eq'
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError('TR Dizin search response lacks a valid total count')
            result['total_reported'] = total
            result['pages_fetched'] += 1
            hits = hits_root['hits']
            if not hits:
                if scanned < total:
                    raise ValueError('TR Dizin returned an empty page before its reported total')
                result['status'] = 'completed' if relation == 'eq' else 'capped'
                return result
            newly_seen = 0
            for hit in hits:
                if not isinstance(hit, dict):
                    raise ValueError('TR Dizin returned a non-object search hit')
                row = trdizin_row(hit, keyword)
                key = row['Record_IDs']
                if key in seen:
                    continue
                seen.add(key)
                newly_seen += 1
                if len(result['rows']) < max_results:
                    result['rows'].append(row)
            if not newly_seen:
                raise ValueError('TR Dizin repeated a search page; pagination could not be completed')
            scanned += len(hits)
            if len(result['rows']) >= max_results:
                result['status'] = 'completed' if len(seen) <= max_results and scanned >= total and relation == 'eq' else 'capped'
                return result
            if scanned >= total and relation == 'eq':
                result['status'] = 'completed'
                return result
        result['status'] = 'capped'
    except Exception as exc:
        result['status'] = 'blocked' if getattr(exc, 'blocked', False) else 'failed'
        result['error'] = type(exc).__name__ + ': ' + str(exc)
    return result


"""Direct DergiPark search adapters. Search may require a manual Turnstile check.

The HTTP endpoint and active verification page were observed on 2026-11-12.
Search-card selectors match DergiPark's published markup as documented by a
current browser scraper. Pagination follows the actual next link when present.
No CAPTCHA token is generated, solved, or bypassed by this module.
"""
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

DERGIPARK_ORIGIN = "https://dergipark.org.tr"


def dergipark_search_url(keyword, page=1):
    params = {"q": keyword, "section": "article"}
    if page > 1:
        params["page"] = page
    return DERGIPARK_ORIGIN + "/tr/search?" + urlencode(params)


def dergipark_gate(html, url="", status=200):
    path = urlsplit(url).path.rstrip("/").lower()
    if path.endswith("/verification") or re.search(
            r"name=[\"']search_verification[\"']|class=[\"'][^\"']*cf-turnstile|"
            r"<title[^>]*>[^<]*(?:Arama Doğrulama|Just a moment)|cf-chl-", html, re.I):
        return "DergiPark requires human verification. Run with --browser and complete the check in the visible browser."
    if path.endswith("/login") or re.search(r"giriş yapmanız gerekmektedir|login_check", html, re.I):
        return "DergiPark requires sign-in. Run with --browser and sign in in the visible browser."
    if status in (401, 403, 429):
        return "DergiPark access blocked or rate limited (HTTP %s)" % status
    return ""


def dergipark_parse_search(html, page_url, keyword):
    """Return cards, next link, total and explicit-empty evidence."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.card.article-card.dp-card-outline, .article-card")
    rows, seen = [], set()
    for card in cards:
        link = card.select_one("h5.card-title > a[href], .card-title a[href]")
        if link is None:
            link = card.select_one('a[href*="/article/"], a[href*="/issue/"]')
        if link is None:
            continue
        url = urljoin(page_url, link.get("href", ""))
        title = link.get_text(" ", strip=True)
        if not title or url in seen or urlsplit(url).hostname != "dergipark.org.tr":
            continue
        seen.add(url)
        id_match = re.search(r"/(?:article/|issue/\d+/)(\d+)(?:[/?#]|$)", url)
        authors = [node.get_text(" ", strip=True) for node in card.select(".article-authors a, .authors a")]
        rows.append({"Title": title, "Authors": "; ".join(authors),
                     "Year": "", "DOI": "",
                     "Link": url, "Source": "DergiPark", "Query": keyword,
                     "Abstract": "", "Journal": "", "Document_type": "article",
                     "Record_IDs": "DergiPark:" + id_match.group(1) if id_match else ""})
    text = soup.get_text(" ", strip=True)
    explicit_empty = bool(re.search(
        r"sonuç bulunamadı|sonuc bulunamadi|sonuç bulunamamıştır|sonuç bulunmamaktadır|"
        r"arama sonucu bulunamadı|no results(?: found)?|no articles found|\b0\s+(?:sonuç|results)\b", text, re.I))
    total = None
    for selector in (".search-results-title", ".search-result-count", ".result-count", "h1", "h2", "h3", "h4"):
        for node in soup.select(selector):
            match = re.search(r"([\d.,]+)\s+(?:sonuç|results|makale)\b", node.get_text(" ", strip=True), re.I)
            if match:
                total = int(re.sub(r"\D", "", match.group(1)))
                break
        if total is not None:
            break
    if explicit_empty:
        total = 0
    next_url = ""
    next_link = soup.select_one('a[rel="next"][href], li.next:not(.disabled) a[href], .page-item.next:not(.disabled) a[href]')
    if next_link:
        next_url = urljoin(page_url, next_link["href"])
    else:
        current_page = int(parse_qs(urlsplit(page_url).query).get("page", ["1"])[0])
        candidates = []
        for link in soup.select(".pagination a[href]"):
            url = urljoin(page_url, link["href"])
            query = parse_qs(urlsplit(url).query)
            try:
                page = int(query.get("page", ["0"])[0])
            except (ValueError, TypeError):
                continue
            if page > current_page:
                candidates.append((page, url))
        if candidates:
            next_url = min(candidates)[1]
    if next_url:
        parts = urlsplit(next_url)
        if parts.hostname != "dergipark.org.tr" or parts.path != "/tr/search":
            next_url = ""
        else:
            params = parse_qs(parts.query)
            params["q"] = [keyword]
            params["section"] = ["article"]
            next_url = DERGIPARK_ORIGIN + "/tr/search?" + urlencode(params, doseq=True)
    return {"rows": rows, "next_url": next_url, "total_reported": total,
            "explicit_empty": explicit_empty, "recognized_cards": bool(cards)}


def dergipark_parse_detail(html, url, keyword):
    soup = BeautifulSoup(html, "html.parser")
    meta = {}
    for node in soup.select("meta[name][content]"):
        key = node["name"].casefold()
        meta.setdefault(key, []).append(node["content"].strip())
    def first(*keys):
        return next((meta[key.casefold()][0] for key in keys if meta.get(key.casefold())), "")
    title = first("citation_title", "DC.Title")
    if not title:
        raise ValueError("Article page lacks citation_title/DC.Title metadata")
    date = first("citation_publication_date", "DC.Date")
    year = re.search(r"\b(?:19|20)\d{2}\b", date)
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", first("citation_doi", "DC.Identifier.DOI"), flags=re.I).strip()
    if doi and not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        doi = ""
    authors = meta.get("citation_author") or meta.get("dc.creator.personalname") or []
    pdf = first("citation_pdf_url")
    return {"Title": title, "Authors": "; ".join(dict.fromkeys(authors)),
            "Year": year.group() if year else "", "DOI": doi.lower(),
            "Link": first("citation_abstract_html_url", "DC.Identifier.URI") or url,
            "Source": "DergiPark", "Query": keyword,
            "Abstract": first("citation_abstract", "DC.Description"),
            "Journal": first("citation_journal_title", "DC.Source"),
            "Document_type": "article", "OA_PDF": urljoin(url, pdf) if pdf else "",
            "Volume": first("citation_volume"), "Issue": first("citation_issue"),
            "Keywords": first("citation_keywords"), "Language": first("citation_language")}


def dergipark_enrich_row(session, row, keyword, timeout):
    response = None
    try:
        response = session.get(row["Link"], timeout=timeout)
        html = response.text
        gate = dergipark_gate(html, response.url, response.status_code)
        if gate:
            raise RuntimeError(gate)
        response.raise_for_status()
        row.update(dergipark_parse_detail(html, response.url, keyword))
    except Exception as exc:
        # A search hit remains useful even when its metadata page is unavailable.
        row["Metadata_error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        if response is not None:
            response.close()
    return row


def search_dergipark(session, keyword, max_results=50, max_pages=5, timeout=30):
    url = dergipark_search_url(keyword)
    result = {"rows": [], "status": "failed", "error": "", "search_url": url,
              "pages_fetched": 0, "total_reported": None}
    seen, visited = set(), set()
    try:
        while url and result["pages_fetched"] < max_pages:
            if url in visited:
                raise RuntimeError("DergiPark repeated a pagination URL")
            visited.add(url)
            with session.get(url, timeout=timeout) as response:
                html = response.text
                gate = dergipark_gate(html, response.url, response.status_code)
                if gate:
                    result.update(status="blocked", error=gate)
                    return result
                response.raise_for_status()
                result["pages_fetched"] += 1
                parsed = dergipark_parse_search(html, response.url, keyword)
            if result["total_reported"] is None:
                result["total_reported"] = parsed["total_reported"]
            if not parsed["rows"] and not parsed["explicit_empty"]:
                raise ValueError("Unrecognized DergiPark results HTML; zero results cannot be confirmed")
            added = 0
            for row in parsed["rows"]:
                if row["Link"] in seen:
                    continue
                if len(result["rows"]) >= max_results:
                    result["status"] = "capped"
                    return result
                seen.add(row["Link"])
                result["rows"].append(dergipark_enrich_row(session, row, keyword, timeout))
                added += 1
            url = parsed["next_url"]
            if url and added == 0:
                raise RuntimeError("DergiPark pagination repeated previously returned articles")
            if len(result["rows"]) >= max_results:
                result["status"] = "capped" if url or len(parsed["rows"]) > added or (result["total_reported"] or 0) > len(result["rows"]) else "completed"
                return result
        result["status"] = "capped" if url or (result["total_reported"] or 0) > len(result["rows"]) else "completed"
    except Exception as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
    return result


def search_dergipark_browser(page, session, keyword, max_results=50, max_pages=5,
                             timeout=30, verification_timeout=180):
    """Visible-browser search with manual verification; never solves a challenge."""
    url = dergipark_search_url(keyword)
    result = {"rows": [], "status": "failed", "error": "", "search_url": url,
              "pages_fetched": 0, "total_reported": None}
    seen, visited = set(), set()
    prompted = False
    try:
        while url and result["pages_fetched"] < max_pages:
            if url in visited:
                raise RuntimeError("DergiPark repeated a pagination URL")
            visited.add(url)
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            html = page.content()
            gate = dergipark_gate(html, page.url, response.status if response else 200)
            if gate:
                if not prompted and verification_timeout > 0:
                    print("DergiPark requires a manual check/sign-in. Complete it in the visible browser; the script will resume automatically.", flush=True)
                    prompted = True
                deadline = time.monotonic() + verification_timeout
                while gate and time.monotonic() < deadline:
                    page.wait_for_timeout(1000)
                    html = page.content()
                    gate = dergipark_gate(html, page.url)
                if gate:
                    result.update(status="blocked", error=gate)
                    return result
                query = parse_qs(urlsplit(page.url).query)
                if query.get("q") != [keyword] or urlsplit(page.url).path != "/tr/search":
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                    html = page.content()
                    gate = dergipark_gate(html, page.url)
                    if gate:
                        result.update(status="blocked", error=gate)
                        return result
            result["pages_fetched"] += 1
            parsed = dergipark_parse_search(html, page.url, keyword)
            if result["total_reported"] is None:
                result["total_reported"] = parsed["total_reported"]
            if not parsed["rows"] and not parsed["explicit_empty"]:
                raise ValueError("Unrecognized DergiPark browser results; zero results cannot be confirmed")
            added = 0
            for row in parsed["rows"]:
                if row["Link"] in seen:
                    continue
                if len(result["rows"]) >= max_results:
                    result["status"] = "capped"
                    return result
                seen.add(row["Link"])
                result["rows"].append(dergipark_enrich_row(session, row, keyword, timeout))
                added += 1
            url = parsed["next_url"]
            if url and not added:
                raise RuntimeError("DergiPark pagination repeated previously returned articles")
            if len(result["rows"]) >= max_results:
                result["status"] = "capped" if url or len(parsed["rows"]) > added or (result["total_reported"] or 0) > len(result["rows"]) else "completed"
                return result
        result["status"] = "capped" if url or (result["total_reported"] or 0) > len(result["rows"]) else "completed"
    except Exception as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
    return result




def merge_results(rows):
    """Merge DOI or stable portal identities, including later DOI enrichment."""
    merged = []

    def same_record(left, right):
        a, b = left.get('DOI', ''), right.get('DOI', '')
        if a and b:
            return a == b
        same_source = left.get('Source', '') == right.get('Source', '')
        for field in ('Thesis_ID', 'Record_IDs'):
            av, bv = left.get(field, ''), right.get(field, '')
            if same_source and av and bv:
                if set(av.split(' | ')) & set(bv.split(' | ')):
                    return True
                # Distinct native identifiers must not merge through a shared search URL.
                return False
        left_links = set(v for v in left.get('Links', left.get('Link', '')).split(' | ') if v)
        right_links = set(v for v in right.get('Links', right.get('Link', '')).split(' | ') if v)
        if left_links & right_links:
            return True
        if same_source and not left_links and not right_links:
            return (unicodedata.normalize('NFKC', left.get('Title', '')).casefold(), left.get('Year', '')) == (
                unicodedata.normalize('NFKC', right.get('Title', '')).casefold(), right.get('Year', ''))
        return False

    def combine(target, incoming):
        for field in ('Source', 'Query', 'Links', 'Record_IDs'):
            values = list(dict.fromkeys(v for text in (target.get(field, ''), incoming.get(field, ''))
                                       for v in text.split(' | ') if v))
            target[field] = ' | '.join(values)
        for field, value in incoming.items():
            if not target.get(field) and value:
                target[field] = value

    for incoming in rows:
        row = {key: '' if value is None else str(value) for key, value in incoming.items()}
        row['DOI'] = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '',
                            unquote(row.get('DOI', '')).strip(), flags=re.I).lower()
        row['Links'] = row.get('Links') or row.get('Link', '')
        matches = [index for index, target in enumerate(merged) if same_record(target, row)]
        if not matches:
            merged.append(row)
            continue
        target = merged[matches[0]]
        combine(target, row)
        for index in reversed(matches[1:]):
            combine(target, merged.pop(index))
    return merged


def write_search_outputs(path, rows, audit):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    audit_path = path.with_suffix('.audit.json')
    if audit_path.resolve() == path.resolve():
        raise ValueError('--out must use a .csv extension, not .audit.json')
    temporary_paths = []
    try:
        for destination, kind in ((path, 'csv'), (audit_path, 'json')):
            fd, filename = tempfile.mkstemp(prefix='.turkish-search-', suffix='.' + kind, dir=path.resolve().parent)
            temporary = Path(filename)
            temporary_paths.append(temporary)
            with os.fdopen(fd, 'w', encoding='utf-8-sig' if kind == 'csv' else 'utf-8', newline='') as handle:
                if kind == 'csv':
                    fields = list(dict.fromkeys(CSV_FIELDS + [k for row in rows for k in row]))
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                else:
                    json.dump(audit, handle, ensure_ascii=False, indent=2)
        os.replace(temporary_paths[0], path)
        os.replace(temporary_paths[1], audit_path)
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Example: python turkish_literature.py "yeraltı suyu" "besin tuzu" --out turkish_results.csv'))
    parser.add_argument('keywords', nargs='*', help='Keyword phrases; quote phrases containing spaces')
    parser.add_argument('--keywords', '--keyword', dest='keyword_options', nargs='+', action='append', default=[],
                        help='Alternative keyword input, e.g. --keywords "yeraltı suyu" "karst"')
    parser.add_argument('--sources', '--source', default='yok_tez,tr_dizin,dergipark',
                        help='Comma-separated portals: yok_tez,tr_dizin,dergipark (default all three)')
    parser.add_argument('--out', default='turkish_results.csv', help='Output CSV (UTF-8 with BOM for Excel)')
    parser.add_argument('--max-results', '--max', type=int, default=50, help='Result limit PER keyword PER source (default 50)')
    parser.add_argument('--max-pages', type=int, default=5, help='Search-page limit per keyword/source (default 5)')
    parser.add_argument('--year-from', type=int, help='Filter retrieved metadata by publication year; unknown years are retained')
    parser.add_argument('--delay', type=float, default=1, help='Seconds between HTTP requests (default 1)')
    parser.add_argument('--timeout', type=float, default=30, help='HTTP timeout in seconds (default 30)')
    parser.add_argument('--browser', action='store_true', help='Open a visible Chromium browser if DergiPark asks for verification')
    parser.add_argument('--browser-profile', default='.turkish_browser', help='Folder to retain DergiPark browser session (default .turkish_browser)')
    parser.add_argument('--verification-timeout', type=float, default=180, help='Seconds to allow manual DergiPark verification (default 180)')
    parser.add_argument('--overwrite', action='store_true', help='Replace existing result CSV and audit')
    args = parser.parse_args(argv)
    keywords = [value.strip() for value in args.keywords + [v for group in args.keyword_options for v in group] if value.strip()]
    if not keywords and sys.stdin.isatty():
        phrase = input('Enter a Turkish/English keyword phrase to search: ').strip()
        if phrase:
            keywords = [phrase]
    keywords = list(dict.fromkeys(keywords))
    if not keywords:
        parser.error('Supply at least one keyword, e.g. "yeraltı suyu"')
    sources = list(dict.fromkeys(s.strip().lower() for s in args.sources.split(',') if s.strip()))
    if not sources or any(source not in PORTALS for source in sources):
        parser.error('--sources must contain yok_tez,tr_dizin,dergipark, separated by commas')
    if args.max_results < 1 or args.max_pages < 1:
        parser.error('--max-results and --max-pages must be positive')
    if not math.isfinite(args.delay) or args.delay < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--delay must be nonnegative and --timeout positive (finite numbers)')
    if not math.isfinite(args.verification_timeout) or args.verification_timeout < 0:
        parser.error('--verification-timeout must be finite and nonnegative')
    out = Path(args.out)
    if out.suffix.lower() != '.csv':
        parser.error('--out must end in .csv')
    if not args.overwrite and (out.exists() or out.with_suffix('.audit.json').exists()):
        parser.error('Output already exists; use a new --out or add --overwrite')
    sessions = {source: SearchSession(args.delay, args.timeout) for source in sources}
    playwright = context = None
    rows, jobs = [], []
    browser_error = ''
    interrupted = False
    adapters = {'yok_tez': search_yok_tez, 'tr_dizin': search_tr_dizin, 'dergipark': search_dergipark}
    try:
        for keyword in keywords:
            for source in sources:
                print(f'Searching {PORTALS[source][0]}: {keyword}', flush=True)
                session = sessions[source]
                first_request = len(session.request_log)
                result = None
                try:
                    result = adapters[source](session, keyword, args.max_results, args.max_pages, args.timeout)
                    if source == 'dergipark' and args.browser and result.get('status') in {'blocked', 'failed'}:
                        if context is None and not browser_error:
                            try:
                                from playwright.sync_api import sync_playwright
                                playwright = sync_playwright().start()
                                context = playwright.chromium.launch_persistent_context(
                                    user_data_dir=str(Path(args.browser_profile).resolve()), headless=False,
                                    locale='tr-TR', accept_downloads=False)
                            except Exception as exc:
                                browser_error = ('Cannot start visible browser: ' + str(exc) +
                                    '. Install with: python -m pip install playwright; python -m playwright install chromium')
                        if context is not None:
                            page = context.pages[0] if context.pages else context.new_page()
                            http_result = result
                            browser_result = search_dergipark_browser(page, session, keyword, args.max_results, args.max_pages,
                                                                     args.timeout, verification_timeout=args.verification_timeout)
                            result = dict(browser_result)
                            result['attempts'] = [
                                {'mode': mode, 'retrieved': len(attempt.get('rows', [])),
                                 **{key: value for key, value in attempt.items() if key != 'rows'}}
                                for mode, attempt in (('http', http_result), ('browser', browser_result))]
                            combined = merge_results(http_result.get('rows', []) + browser_result.get('rows', []))
                            result['rows'] = combined[:args.max_results]
                            result['pages_fetched'] = http_result.get('pages_fetched', 0) + browser_result.get('pages_fetched', 0)
                            result['total_reported'] = browser_result.get('total_reported') if browser_result.get('total_reported') is not None else http_result.get('total_reported')
                            if len(combined) > args.max_results and result['status'] == 'completed':
                                result['status'] = 'capped'
                            if http_result.get('rows') and not browser_result.get('rows') and result['status'] == 'completed':
                                result.update(status='failed', error='Browser returned zero after HTTP returned records; earlier records were preserved.')
                        else:
                            result['error'] = (result.get('error', '') + '; ' + browser_error).strip('; ')
                except Exception as exc:
                    error = f'{type(exc).__name__}: {exc}'
                    blocked = bool(re.search(r'403|418|429|captcha|verification|challenge|blocked', error, re.I))
                    partial = result.get('rows', []) if isinstance(result, dict) else []
                    result = {'rows': partial, 'status': 'blocked' if blocked else 'failed', 'error': error,
                              'search_url': PORTALS[source][1], 'pages_fetched': 0, 'total_reported': None}
                for row in result.get('rows', []):
                    row.setdefault('Source', PORTALS[source][0])
                    row.setdefault('Query', keyword)
                    rows.append(row)
                job = {key: value for key, value in result.items() if key != 'rows'}
                job.update(source=source, keyword=keyword, retrieved=len(result.get('rows', [])),
                           requests=session.request_log[first_request:])
                jobs.append(job)
                print(f"  {len(result.get('rows', []))} records; {result['status']}" +
                      (f"; {result['error']}" if result.get('error') else ''), flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print('\nInterrupted; saving records collected so far.', file=sys.stderr)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        for session in sessions.values():
            session.close()
    raw_count = len(rows)
    if args.year_from:
        rows = [row for row in rows if not re.search(r'\b\d{4}\b', str(row.get('Year', '')))
                or int(re.search(r'\b\d{4}\b', str(row.get('Year', ''))).group()) >= args.year_from]
    rows = merge_results(rows)
    audit = {'completed_at': datetime.now(timezone.utc).isoformat(), 'keywords': keywords, 'sources': sources,
             'max_results_per_keyword_source': args.max_results, 'max_pages': args.max_pages,
             'year_from': args.year_from, 'retrieved_before_year_filter': raw_count,
             'exported': len(rows), 'interrupted': interrupted, 'jobs': jobs,
             'limitations': ['Keyword semantics and metadata coverage differ between portals.',
                            'Result/page limits can truncate searches; capped is not an exhaustive search.',
                            'DergiPark may require manual verification with --browser.',
                            'Publication year filtering is applied after retrieval; unknown years are retained.',
                            'DOI-less theses remain in results and cannot be sent to a DOI PDF downloader.']}
    try:
        write_search_outputs(out, rows, audit)
    except (OSError, ValueError) as exc:
        print(f'Cannot save results: {exc}', file=sys.stderr)
        return 2
    print(f'Saved {len(rows)} unique records to {out}; audit: {out.with_suffix(".audit.json")}')
    unsuccessful = [job for job in jobs if job['status'] not in {'completed', 'capped'}]
    if unsuccessful:
        print('Some searches failed or were blocked. Successful-source results were saved; see the audit.', file=sys.stderr)
    return 130 if interrupted else (1 if unsuccessful else 0)


if __name__ == '__main__':
    raise SystemExit(main())
