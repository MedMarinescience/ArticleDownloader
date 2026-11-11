# Literature search and Sci-Hub PDF integration

Requires Python 3.10 or newer. Extract **all files into the same folder**, open a terminal there, then install:

```bash
python -m pip install -r requirements.txt
```

## Download a selected CSV

Use a CSV with a `DOI` column; `Title` and other columns are retained. UTF-8 BOM, comma, semicolon and tab separators are supported. Missing DOIs are reported per row.

```bash
python search_articles7.py --from-csv selected.csv --download-pdfs --pdf-dir selected_pdfs --out selected_with_pdfs.csv
```

This now requests Sci-Hub directly. To select mirrors explicitly, append `--mirror https://YOUR-MIRROR-HOST`; repeat the flag for fallback mirrors. Use an actual HTTPS mirror hostname, not a DOI URL, LibGen URL, or the placeholder above. Alternatively set `SCIHUB_MIRRORS` to comma-separated HTTPS origins.

The initial candidate mirrors come from the linked scihub-cli configuration: `.ren`, `.ee`, `.mk`. They are **not verified working mirrors**. During this repair, live checks were blocked by the execution environment's network proxy (403), so no real Sci-Hub article download was verified. Automated tests use generated PDFs and simulated HTTP responses. Mirror reachability and article availability depend on your network and the mirror.

Optional providers:

```bash
# Unpaywall first, then Sci-Hub for unsuccessful rows:
python search_articles7.py --from-csv selected.csv --download-pdfs --pdf-provider both --out selected_both.csv

# Unpaywall only (requires UNPAYWALL_EMAIL in your environment):
python search_articles7.py --from-csv selected.csv --download-pdfs --pdf-provider unpaywall --out selected_oa.csv
```

Existing keyword/author searches still collect metadata. For the main search script, PDF downloading requires `--from-csv` so the reviewed selection determines which PDFs to request.

## Standalone CSV or DOI downloader

`search_articles8.py` is now valid Python and calls the shared downloader. It is a **PDF downloader**, while `search_articles7.py` remains the metadata search application.

```bash
python search_articles8.py download --file selected.csv --source csv --count 100 -o selected_pdfs --report selected_pdf_report.csv
python search_articles8.py download --doi 10.1234/your-real-article-doi -o selected_pdfs --report single_pdf_report.csv
python search_articles8.py download --file doi_list.txt --source list -o selected_pdfs --report list_pdf_report.csv
```

The DOI in the second command is a placeholder; replace it with the article's DOI. Commands can also use `--mirror`, `--scihub-delay` (default 2 seconds), `--pdf-timeout` (default 30 seconds), and `--pdf-max-mb` (default 100 MiB). `--count` limits rows processed, rather than promising that many successful downloads. The report must be a new path.

`python search_articles8.py check-domains --out mirror_status.json` checks configured mirror homepages. The old `update-domains` name is an alias. It now reports reachability and recognizable Sci-Hub markup; it does not claim to discover domains or prove that a PDF is available.

## Replacement for the pasted integration script

Save/use `selected_pdf_integration.py` instead of running the pasted text:

```bash
python selected_pdf_integration.py --from-csv selected.csv --pdf-dir selected_pdfs --out selected_scihub.csv
```

Its default target is the included `search_articles7.py`. Existing `--search-script`, repeatable `--mirror`, `--delay`, `--max-mb` and `--enrich-abstracts` flags remain available; abstract enrichment makes separate metadata-service requests. This wrapper uses the main script's native provider option instead of replacing a function at runtime. Unlike the old pasted version, it writes one normal result CSV and a `.audit.json` file rather than separate `.scihub.csv` and `.scihub.audit.json` reports.

## Turkish literature

The Turkish importer still maps Turkish headers and preserves its raw import/provenance log. It can now optionally download PDFs using the DOI from each imported record:

```bash
python turkish_literature.py import --source dergipark --input turkish_export.csv --out turkish_with_pdfs.csv --download-pdfs --pdf-dir turkish_pdfs
```

For semicolon exports append `--delimiter ";"`. Supported sources remain `dergipark`, `tr_dizin`, `yok_tez`; RIS imports also work. Omit `--download-pdfs` for metadata-only import. Thesis IDs and titles are not substituted for DOIs: records without an article DOI get `missing_or_invalid_doi`. This update does not add live portal scraping, translate Turkish search expressions, or promise Sci-Hub coverage of Turkish articles or theses. Existing configured export/OAI searches in the main script are retained.

## What changed

- Replaced mixed Markdown/Python in `search_articles8.py` with a runnable entry point.
- Fixed DOI-specific GET lookup and added homepage-form POST fallback, using a persistent session and the landing page as Referer.
- Added iframe, embed, object, metadata, download-anchor/container, and save-button extraction. Relative and protocol-relative links resolve against the page's final URL; viewer fragments are removed. HTTPS is required.
- Added bounded request retries for 429/5xx responses, request spacing, timeouts, size limits, and mirror fallback. CAPTCHA/challenge pages are reported for manual handling.
- Verify PDF signatures and readable pages with `pypdf`; HTML named `.pdf` never counts as a PDF. Stream to temporary files and publish only after validation. Invalid pre-existing Sci-Hub files are preserved under `.invalid-*` filenames before retrying. Valid existing files can be reused.
- Unique DOI/hash filenames replace the broken filename logic that returned the literal name `pdf`. Duplicate DOIs share one download within a batch while CSV rows/notes stay separate.
- Added explicit per-row status, provider, actual PDF URL, local path, SHA-256 checksum and DOI-text check. Sci-Hub results are not labeled as open access.

`downloaded` means a readable PDF with the requested DOI found in text extracted from its first three pages. `downloaded_needs_review` means a readable PDF whose DOI was not confirmed there; review its title/authors and contents. `already_present` means a local PDF passed validation; consult `PDF_DOI_check` because its identity may remain unconfirmed. Cached files with unknown origins are labeled `Local cache`; matching path/checksum provenance from a previous CSV is preserved when available. The DOI check is evidence, not a guarantee of article identity, and other DOIs may appear in references. `not_downloaded`, `failed` and `missing_or_invalid_doi` are unsuccessful outcomes; see `PDF_error` for the specific mirror errors. Standalone and reviewed-selection flows return exit code 1 when any requested row is unsuccessful, 2 for configuration/input errors, and 0 when all requested rows succeed. Hashes detect file changes, not whether the correct article was returned.

The main script also writes its existing metadata/harvest/audit sidecars. Main-script CSV output paths follow its existing overwrite behavior; choose a new `--out` to preserve previous runs. The wrapper and Turkish importer reject existing output paths. Shared PDF files are never overwritten in place.

## Verification

```bash
python -m unittest discover -s tests -v
```

The suite uses synthetic one-page PDFs and mocked responses; it downloads no real article and does not require mirror access.

## Reference implementations reviewed

This is newly written integration code based on the observed interfaces, rather than a copy of a third-party downloader:

- https://github.com/SJRnhqh/DoiHive — GET DOI lookup; object/download link patterns.
- https://github.com/Tishacy/SciDownl — POST `request` lookup; embedded PDF extraction.
- https://github.com/Oxidane-bot/scihub-cli — DOI lookup; button/iframe parser patterns; candidate mirror configuration.
- https://suqingdong.github.io/scihub/ and https://github.com/suqingdong/scihub — homepage form action discovery and POST lookup.
