#!/usr/bin/env python3
"""Resolve article DOIs through Sci-Hub GET/POST interfaces and verify PDFs."""
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
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit

import requests
from pypdf import PdfReader

# Starting candidates, not a promise of availability. Override with --mirror or
# SCIHUB_MIRRORS (comma-separated). No unrelated sites are used as mirrors.
DEFAULT_MIRRORS = ("https://sci-hub.ren", "https://sci-hub.ee", "https://sci-hub.mk")
SCIHUB_BASES = DEFAULT_MIRRORS
PDF_FIELDS = ["PDF_status", "PDF_provider", "PDF_URL", "Pdf_path",
              "PDF_DOI_check", "PDF_SHA256", "PDF_error"]
SUCCESS_STATUSES = {"downloaded", "downloaded_needs_review", "already_present"}
DOI_PATTERN = re.compile(r"10\.\d{4,9}/\S+", re.I)
USER_AGENT = "Mozilla/5.0 (compatible; LiteraturePDFDownloader/2.0)"


def normalize_doi(value):
    value = unquote(str(value or "").strip())
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value,
                  flags=re.I).strip().lower()


def valid_https(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and bool(parts.hostname)
                and not parts.username and not parts.password)
    except ValueError:
        return False


def configured_mirrors(mirrors=None):
    if mirrors is None:
        mirrors = [s.strip() for s in os.environ.get("SCIHUB_MIRRORS", "").split(",") if s.strip()]
        mirrors = mirrors or DEFAULT_MIRRORS
    if isinstance(mirrors, str):
        mirrors = [mirrors]
    result = []
    for value in mirrors:
        base = str(value).strip().rstrip("/")
        parts = urlsplit(base)
        if not valid_https(base) or parts.query or parts.fragment or parts.path not in ("", "/"):
            raise ValueError("Each mirror must be an HTTPS origin, e.g. https://mirror.example")
        if base not in result:
            result.append(base)
    if not result:
        raise ValueError("At least one Sci-Hub mirror is required")
    return result


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_pdf_filename(doi):
    doi = normalize_doi(doi)
    slug = re.sub(r"[^a-z0-9._-]+", "_", doi)[:100].strip(".") or "article"
    return f"{slug}-{hashlib.sha256(doi.encode()).hexdigest()[:12]}.pdf"


def session_with_scihub_headers():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5"})
    return session


class PDFLinks(HTMLParser):
    """Extract links without executing JavaScript or relying on a single layout."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.forms = []
        self.base = ""
        self._stack = []
        self._anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or "" for key, value in attrs}
        download_context = bool(self._stack and self._stack[-1][1]) or bool(
            re.search(r"download|save", attrs.get("class", "") + " " + attrs.get("id", ""), re.I))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._stack.append((tag, download_context))
        if tag == "base":
            self.base = attrs.get("href", "")
        if tag in ("iframe", "embed", "object"):
            self.links.append(attrs.get("src") or attrs.get("data") or "")
        elif tag == "meta" and (attrs.get("name", "").lower() == "citation_pdf_url"
                                 or attrs.get("property", "").lower() == "citation_pdf_url"):
            self.links.append(attrs.get("content", ""))
        elif tag == "a":
            href = attrs.get("href", "")
            self._anchor = (href, [])
            if (re.search(r"\.pdf(?:[?#]|$)", href, re.I) or "download" in attrs or download_context
                    or re.search(r"/downloads?(?:[/?#]|$)", href, re.I)
                    or re.search(r"download|save", attrs.get("class", "") + " " + attrs.get("id", ""), re.I)):
                self.links.append(href)
        elif tag == "form" and attrs.get("method", "get").lower() == "post":
            self.forms.append(attrs.get("action", ""))
        onclick = attrs.get("onclick", "")
        for match in re.finditer(r"(?:window\.)?location(?:\.href)?\s*=\s*(['\"])(.*?)\1", onclick, re.I):
            self.links.append(match.group(2).replace(r"\/", "/"))

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            href, texts = self._anchor
            if re.search(r"download|save|скачать|下载|indir", " ".join(texts), re.I):
                self.links.append(href)
            self._anchor = None
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor[1].append(data)


class SciHubDownloader:
    def __init__(self, mirrors=None, pdf_dir="selected_pdfs", delay=2.0, max_mb=100, timeout=30):
        self.mirrors = configured_mirrors(mirrors)
        if not all(math.isfinite(float(v)) for v in (delay, max_mb, timeout)):
            raise ValueError("Delay, size and timeout must be finite")
        if delay < 0 or max_mb <= 0 or timeout <= 0:
            raise ValueError("Delay must be nonnegative; size and timeout must be positive")
        self.folder = Path(pdf_dir).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.delay = float(delay)
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.timeout = float(timeout)
        self.last_request = None
        self.session = session_with_scihub_headers()

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get(self, url, method="GET", data=None, referer=None):
        if not valid_https(url):
            raise ValueError("Only HTTPS links are accepted")
        for attempt in range(3):
            if self.last_request is not None:
                wait = self.delay - (time.monotonic() - self.last_request)
                if wait > 0:
                    time.sleep(wait)
            self.last_request = time.monotonic()
            response = self.session.request(method, url, data=data,
                                            headers={"Referer": referer} if referer else {},
                                            stream=True, timeout=(min(15, self.timeout), self.timeout))
            if not valid_https(response.url):
                response.close()
                raise ValueError("Response redirected to a non-HTTPS URL")
            if response.status_code == 429 or response.status_code >= 500:
                status = response.status_code
                after = response.headers.get("Retry-After", "")
                response.close()
                if attempt == 2:
                    raise RuntimeError(f"HTTP {status}; retries exhausted")
                try:
                    wait = float(after)
                except ValueError:
                    try:
                        wait = (parsedate_to_datetime(after) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        wait = 2 ** attempt
                time.sleep(min(max(wait, 0), 60) if math.isfinite(wait) else 2 ** attempt)
                continue
            if response.status_code != 200:
                status = response.status_code
                response.close()
                raise RuntimeError(f"HTTP {status}")
            return response
        raise RuntimeError("Request failed")

    @staticmethod
    def inspect(path, doi):
        with Path(path).open("rb") as handle:
            if b"%PDF-" not in handle.read(1024):
                raise ValueError("Downloaded content is not a PDF")
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a password")
        if not len(reader.pages):
            raise ValueError("PDF has no pages")
        text = ""
        for page in reader.pages[:3]:
            try:
                text += "\n" + (page.extract_text() or "")
            except Exception:
                pass
        found = {normalize_doi(m).rstrip(".,;") for m in
                 re.findall(r"10\.\d{4,9}/[^\s<>\"']+", text, re.I)}
        expected = normalize_doi(doi)
        confirmed = bool(expected) and any(item == expected or item.rstrip(")]}.") == expected for item in found)
        return "doi_seen_in_first_three_pages" if confirmed else "doi_not_confirmed"

    def _save_response(self, response, destination, doi, chunks=None):
        destination = Path(destination)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.folder, prefix=".partial-", suffix=".pdf", delete=False) as handle:
                temporary = Path(handle.name)
                size = 0
                for chunk in chunks if chunks is not None else response.iter_content(65536):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError("PDF exceeds size limit")
                    handle.write(chunk)
            check = self.inspect(temporary, doi)
            digest = sha256_file(temporary)
            # Atomic publication without overwriting a different existing file.
            try:
                os.link(temporary, destination)
            except FileExistsError:
                existing_check = self.inspect(destination, doi)
                return {"PDF_status": "already_present", "PDF_provider": "Local cache",
                        "Pdf_path": str(destination), "PDF_DOI_check": existing_check,
                        "PDF_SHA256": sha256_file(destination), "PDF_URL": response.url}
            return {"PDF_status": "downloaded" if check != "doi_not_confirmed" else "downloaded_needs_review",
                    "PDF_provider": "SciHub", "PDF_URL": response.url, "Pdf_path": str(destination),
                    "PDF_DOI_check": check, "PDF_SHA256": digest}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(self, url, destination, doi, referer=None):
        with self.get(url, referer=referer) as response:
            return self._save_response(response, destination, doi)

    def _landing(self, response, destination, doi):
        chunks = response.iter_content(16384)
        prefix = bytearray()
        while len(prefix) < 1024:
            chunk = next(chunks, b"")
            if not chunk:
                break
            prefix.extend(chunk)
        if b"%PDF-" in prefix[:1024]:
            from itertools import chain
            return self._save_response(response, destination, doi, chain([prefix], chunks)), None
        for chunk in chunks:
            prefix.extend(chunk)
            if len(prefix) > 2 * 1024 * 1024:
                raise ValueError("Landing page exceeds size limit")
        text = prefix.decode(response.encoding or "utf-8", errors="replace")
        parser = PDFLinks()
        parser.feed(text)
        base = urljoin(response.url, parser.base) if parser.base else response.url
        links = list(dict.fromkeys(urldefrag(urljoin(base, unescape(s)))[0]
                                  for s in parser.links if s.strip()))
        links = [url for url in links if valid_https(url)]
        forms = [urljoin(response.url, action) for action in parser.forms]
        # Only submit an identifier to a form on the configured mirror's origin.
        forms = [url for url in forms if urlsplit(url).netloc == urlsplit(response.url).netloc and valid_https(url)]
        challenge = re.search(
            r"cf-chl-|verify (?:that )?you are human|just a moment|"
            r"<(?:iframe|form|div)[^>]+(?:recaptcha|hcaptcha|h-captcha|turnstile)|"
            r"<title[^>]*>[^<]*captcha", text, re.I)
        # A Cloudflare CDN script or dormant CAPTCHA script on an article page
        # is not by itself an active challenge.
        if challenge or (not links and re.search(r"captcha|cloudflare", text, re.I)):
            note = "challenge page; manual browser interaction required"
            links = []
        elif links:
            note = ""
        elif re.search(r"article (?:not found|is not available)|not found in (?:the )?database|does(?:n.t| not) have", text, re.I):
            note = "article unavailable"
        else:
            note = "no PDF link found"
        return None, (links[:10], forms, note)

    def download(self, doi):
        doi = normalize_doi(doi)
        if not DOI_PATTERN.fullmatch(doi):
            return {"PDF_status": "missing_or_invalid_doi", "PDF_provider": "SciHub",
                    "PDF_error": "A valid article DOI is required"}
        destination = self.folder / safe_pdf_filename(doi)
        errors = []
        if destination.exists():
            try:
                check = self.inspect(destination, doi)
                return {"PDF_status": "already_present", "PDF_provider": "Local cache",
                        "Pdf_path": str(destination), "PDF_DOI_check": check,
                        "PDF_SHA256": sha256_file(destination)}
            except Exception as exc:
                # Preserve a corrupt previous download, but allow recovery this run.
                quarantine = destination.with_name(destination.name + ".invalid-" + uuid.uuid4().hex[:8])
                destination.rename(quarantine)
                errors.append(f"Invalid existing PDF preserved as {quarantine.name}: {exc}")
        for mirror in self.mirrors:
            post_url = mirror + "/"
            referer = mirror + "/"
            found_form = False
            for method in ("GET", "POST"):
                # Some installations expose the POST action only on the homepage.
                if method == "POST" and not found_form:
                    try:
                        with self.get(mirror + "/", referer=referer) as homepage:
                            result, page = self._landing(homepage, destination, doi)
                            if result:
                                result["PDF_error"] = "; ".join(errors)
                                return result
                            links, forms, note = page
                            if note.startswith("challenge page"):
                                errors.append(f"{urlsplit(mirror).hostname} homepage: {note}")
                                break
                            if forms:
                                post_url = forms[0]
                            referer = homepage.url
                    except Exception as exc:
                        errors.append(f"{urlsplit(mirror).hostname} homepage: {exc}")
                url = mirror + "/" + quote(doi, safe="/") if method == "GET" else post_url
                try:
                    with self.get(url, method=method, data={"request": doi, "sci-hub-plugin-check": ""} if method == "POST" else None,
                                  referer=referer) as response:
                        result, page = self._landing(response, destination, doi)
                        if result:
                            result["PDF_error"] = "; ".join(errors)
                            return result
                        links, forms, note = page
                        referer = response.url
                        if forms:
                            post_url = forms[0]
                            found_form = True
                    for link in links:
                        try:
                            result = self.save(link, destination, doi, referer=referer)
                            result["PDF_error"] = "; ".join(errors)
                            return result
                        except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
                            errors.append(f"{urlsplit(mirror).hostname} PDF: {exc}")
                        except Exception as exc:
                            errors.append(f"{urlsplit(mirror).hostname} PDF: {type(exc).__name__}: {exc}")
                    if note:
                        errors.append(f"{urlsplit(mirror).hostname} {method}: {note}")
                        if note.startswith("challenge page"):
                            break
                except Exception as exc:
                    errors.append(f"{urlsplit(mirror).hostname} {method}: {type(exc).__name__}: {exc}")
        return {"PDF_status": "not_downloaded", "PDF_provider": "SciHub", "PDF_error": "; ".join(errors)}


def download_rows(rows, pdf_dir="selected_pdfs", mirrors=None, delay=2, max_mb=100, timeout=30):
    cache = {}
    failures = 0
    with SciHubDownloader(mirrors, pdf_dir, delay, max_mb, timeout) as downloader:
        for number, row in enumerate(rows, 1):
            doi = normalize_doi(row.get("DOI"))
            if doi not in cache:
                try:
                    cache[doi] = downloader.download(doi)
                except Exception as exc:
                    cache[doi] = {"PDF_status": "failed", "PDF_provider": "SciHub",
                                  "PDF_error": f"{type(exc).__name__}: {exc}"}
            result = dict(cache[doi])
            if (result["PDF_status"] == "already_present" and row.get("Pdf_path")
                    and Path(row["Pdf_path"]).resolve() == Path(result["Pdf_path"]).resolve()
                    and row.get("PDF_SHA256") == result.get("PDF_SHA256")):
                result["PDF_provider"] = row.get("PDF_provider") or result["PDF_provider"]
                result["PDF_URL"] = row.get("PDF_URL", "")
            for field in PDF_FIELDS:
                row[field] = result.get(field, "")
            failures += result["PDF_status"] not in SUCCESS_STATUSES
            print(f"PDF {number}/{len(rows)}: {doi or '(no DOI)'}: {result['PDF_status']}", flush=True)
    return failures


def read_selected(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("CSV is empty")
        headers = [s.strip() for s in reader.fieldnames]
        headers = [{"doi": "DOI", "title": "Title"}.get(s.casefold(), s) for s in headers]
        if not all(headers) or len(headers) != len(set(headers)):
            raise ValueError("CSV requires distinct nonempty headers")
        if "DOI" not in headers:
            raise ValueError("CSV requires a DOI column")
        reader.fieldnames = headers
        rows = list(reader)
        if any(None in row for row in rows):
            raise ValueError("CSV row has extra cells; check quoting/delimiter")
        if not rows:
            raise ValueError("CSV contains no article rows")
        return headers, rows


def write_report(path, headers, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(list(headers) + PDF_FIELDS + [k for row in rows for k in row]))
    with path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def add_scihub_arguments(parser):
    parser.add_argument("--scihub-mirror", "--mirror", action="append", dest="scihub_mirror",
                        help="HTTPS Sci-Hub origin; repeat for fallback mirrors. Overrides SCIHUB_MIRRORS/defaults.")
    parser.add_argument("--scihub-delay", type=float, default=2, help="Seconds between requests (default 2).")
    parser.add_argument("--pdf-max-mb", type=float, default=100, help="Maximum PDF size in MiB (default 100).")
    parser.add_argument("--pdf-timeout", type=float, default=30, help="HTTP timeout in seconds (default 30).")


def download_scihub_pdf(doi, output_dir="selected_pdfs", mirrors=None):
    with SciHubDownloader(mirrors, output_dir) as downloader:
        result = downloader.download(doi)
    return result.get("Pdf_path") if result["PDF_status"] in SUCCESS_STATUSES else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download", help="Download article PDFs from DOIs or a CSV/list.")
    download.add_argument("dois", nargs="*", help="Article DOIs")
    download.add_argument("--doi", action="append", default=[], help="Article DOI; repeat as needed")
    download.add_argument("--file", "--from-csv", dest="file", help="CSV or newline-separated DOI list")
    download.add_argument("-s", "--source", choices=("csv", "list"), help="File format; inferred from extension if omitted")
    download.add_argument("--count", type=int, help="Maximum number of records to process")
    download.add_argument("-o", "--output-dir", "--pdf-dir", default="selected_pdfs", dest="output_dir")
    download.add_argument("--report", default="pdf_download_report.csv", help="New CSV report path")
    add_scihub_arguments(download)
    domains = commands.add_parser("check-domains", aliases=["update-domains"], help="Check configured mirrors; no automatic third-party domain discovery")
    add_scihub_arguments(domains)
    domains.add_argument("--out", default="domain_status.json")
    args = parser.parse_args(argv)
    try:
        if args.command != "download":
            with SciHubDownloader(args.scihub_mirror, delay=args.scihub_delay, max_mb=args.pdf_max_mb, timeout=args.pdf_timeout) as downloader:
                checks = []
                for mirror in downloader.mirrors:
                    try:
                        with downloader.get(mirror + "/") as response:
                            data = bytearray()
                            for chunk in response.iter_content(16384):
                                data.extend(chunk)
                                if len(data) > 512 * 1024:
                                    break
                            looks = bool(re.search(rb"sci[- ]?hub", data, re.I))
                            checks.append({"mirror": mirror, "status": "recognizable_homepage" if looks else "unrecognized_page", "final_url": response.url})
                    except Exception as exc:
                        checks.append({"mirror": mirror, "status": "failed", "error": str(exc)})
            with Path(args.out).open("x", encoding="utf-8") as handle:
                json.dump({"checked_at": datetime.now(timezone.utc).isoformat(), "note": "Homepage checks do not verify article/PDF availability.", "checks": checks}, handle, indent=2)
            print(f"Mirror check report: {args.out}")
            return 0 if any(x["status"] == "recognizable_homepage" for x in checks) else 1
        if args.count is not None and args.count < 1:
            parser.error("--count must be positive")
        if Path(args.report).exists():
            parser.error("Report already exists; choose a new --report")
        headers, rows = ["DOI"], []
        if args.file:
            kind = args.source or ("csv" if Path(args.file).suffix.lower() == ".csv" else "list")
            if kind == "csv":
                headers, rows = read_selected(args.file)
            else:
                rows = [{"DOI": s.strip()} for s in Path(args.file).read_text(encoding="utf-8-sig").splitlines() if s.strip()]
        elif args.source:
            parser.error("--source requires --file")
        rows += [{"DOI": doi} for doi in args.dois + args.doi]
        if not rows:
            parser.error("Supply a DOI, --doi, or --file")
        if args.count:
            rows = rows[:args.count]
        failures = download_rows(rows, args.output_dir, args.scihub_mirror, args.scihub_delay, args.pdf_max_mb, args.pdf_timeout)
        write_report(args.report, headers, rows)
        print(f"Saved {len(rows)-failures}/{len(rows)} PDFs; report: {args.report}")
        return 1 if failures else 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
