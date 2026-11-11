# Two standalone literature tools

There are now **two independent runnable scripts**. Neither imports the other, `search_articles7.py`, an adapter file, or an export helper.

This branch uses a new keyword-search interface for Turkish literature. Do not use the old `import`, `--input export.csv`, or `--turkish-export` commands with this version; use the keyword examples below.

| Script | What it does |
| --- | --- |
| `scihub_downloader.py` | Reads article DOIs from a CSV/list or the command line, tries configured Sci-Hub mirrors, resolves embedded PDF links and validates downloads. |
| `turkish_literature.py` | Sends your keywords directly to **YÖK Tez, TR Dizin and DergiPark**, then exports metadata to CSV. **No `export.csv` is needed.** |

Python 3.10+ is required; Python 3.13 on Windows is supported by the code. The automated execution environment uses Python 3.12/Linux, so Windows execution itself has not been tested.

## 1. Setup in Windows PowerShell

Extract the ZIP, open PowerShell in that folder, then run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Use `.\.venv\Scripts\python.exe` in the commands below. This creates a clean environment separate from the global Requests/urllib3/chardet combination that produced your dependency warning. It does not change or suppress errors in your global Python installation.

## 2. Search Turkish literature by keyword

```powershell
.\.venv\Scripts\python.exe turkish_literature.py "yeraltı suyu" "besin tuzu" --out turkish_results.csv
```

This actually visits all three portals. It does **not** search a local file or just create a pending checklist.

Search only certain portals, or change limits:

```powershell
.\.venv\Scripts\python.exe turkish_literature.py "Göksu" --sources yok_tez,tr_dizin --max-results 100 --max-pages 5 --out goksu_results.csv
```

`--keywords "yeraltı suyu" "karst"` is an alternative to positional keyword phrases. Running with no arguments asks you for a phrase in an interactive terminal.

### DergiPark verification

DergiPark required a real human-verification check in our live test. To let the script open a visible browser, first install this optional feature:

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe -m playwright install chromium
```

Then search with browser fallback enabled:

```powershell
.\.venv\Scripts\python.exe turkish_literature.py "yeraltı suyu" --browser --out turkish_browser_results.csv
```

If DergiPark asks for verification, complete the check in the browser window that opens. The script waits up to 180 seconds and resumes automatically. Increase this with `--verification-timeout 300` if needed. Verification is manual; the script does not solve CAPTCHAs or guarantee that the portal will grant access.

The browser session is kept in `.turkish_browser/` locally. This folder is excluded by `.gitignore`. Normal YÖK/TR Dizin searches use HTTP sessions and do not require Playwright.

### Output and coverage

- `turkish_results.csv`: UTF-8 BOM for Excel; title, authors, year, DOI where supplied, original links, source, keyword, and available metadata. DergiPark detail-page failures keep the search hit with a `Metadata_error` column.
- `turkish_results.audit.json`: a job for every keyword/source, actual requests and HTTP statuses, reported total, returned count, page count, and any block/error.
- `completed`: the adapter reached the portal's available end of results within this query; not proof of catalogue-wide completeness.
- `capped`: result or page limits stopped the query, or the reported total exceeded what was retrieved.
- `blocked` / `failed`: search was not completed. These **never mean zero matching articles**.

Successful-source results are saved even if another source fails. Exit code 1 indicates a blocked/failed job; 0 means all attempted jobs completed or reached their declared caps. An interrupted run saves collected records and returns 130. Input/output errors return 2. Use a new `--out` filename for each run, or add `--overwrite` to replace existing CSV/audit files.

Limits are **per keyword per source**, not the final deduplicated total. `--year-from` filters after retrieval and retains unknown publication years. Turkish characters are retained; keywords are sent as you type them, with each portal applying its own query semantics. Exact DOI matches are deduplicated across portals while source/query/link provenance is retained. DOI-less records use portal identifiers/links for deduplication.

YÖK currently embeds its matches in one HTML response and uses JavaScript to display pages. The script reads those real result cards and metadata without inventing page requests. YÖK thesis abstracts are not fetched separately. TR Dizin's native default search also returns research projects; those are labeled `Document_type=project` rather than articles.

## 3. Download article PDFs from Sci-Hub

Create or supply a real CSV containing a `DOI` column. For example, review the Turkish results and save selected DOI-bearing articles as `selected.csv`. Titles and your other CSV columns are preserved. Theses often have no DOI; a thesis ID is not an article DOI.

```powershell
.\.venv\Scripts\python.exe scihub_downloader.py --csv selected.csv --out pdf_report.csv
```

A single DOI or a newline-separated DOI file also works:

```powershell
.\.venv\Scripts\python.exe scihub_downloader.py --doi 10.1234/REPLACE_WITH_REAL_DOI --out single_pdf_report.csv
.\.venv\Scripts\python.exe scihub_downloader.py --file doi_list.txt --source list --out list_pdf_report.csv
```

The DOI above is a placeholder. Running without arguments uses `selected.csv` if it exists in the current folder; otherwise an interactive terminal prompts for a DOI or file path. The older `download` subcommand is also supported.

Configure mirrors explicitly when needed:

```powershell
.\.venv\Scripts\python.exe scihub_downloader.py --csv selected.csv --mirror https://YOUR_MIRROR_HOST --out pdf_report.csv
```

Replace the placeholder with a real HTTPS Sci-Hub origin. Repeat `--mirror` for fallback mirrors, or set comma-separated origins in the `SCIHUB_MIRRORS` environment variable. Default candidates are `.ren`, `.ee`, `.mk`, taken from the referenced implementation's configuration; **their current availability is not guaranteed**. Rotation tries the configured list; it does not automatically discover new domains. `check-domains --out mirror_status.json` checks homepages, which does not establish article availability.

Options include `--count`, `--pdf-dir` (default `selected_pdfs`), `--scihub-delay` (default 2 seconds), `--pdf-timeout` (default 30 seconds), `--pdf-max-mb` (default 100 MiB), and `--overwrite` for an existing report. Input CSV and report must be different files.

The downloader uses persistent cookies, Referer headers, redirect handling, DOI-specific GET and search-form POST lookup, retries for 429/5xx responses, and iframe/object/embed/download-button parsing. It streams to a temporary file, verifies the PDF signature and readable pages with `pypdf`, then publishes the file. HTML named `.pdf` is rejected. Duplicate DOIs share a request; duplicate CSV rows remain separate. Corrupt existing downloads are preserved as `.invalid-*` before retrying.

Each report row includes `PDF_status`, `PDF_provider`, `PDF_URL`, `Pdf_path`, `PDF_DOI_check`, `PDF_SHA256` and `PDF_error`. A readable PDF whose requested DOI was not found in its first three pages is `downloaded_needs_review`; inspect its title/authors yourself. DOI-text matching is supporting evidence, not a guarantee of identity. Cached files of unknown origin are labeled `Local cache`. Missing DOI, no PDF, blocked mirror, invalid PDF, and network failures are unsuccessful rows. Exit code is 1 if any requested row is unsuccessful, 2 for input/configuration errors, otherwise 0.

## What was actually verified

Live checks performed during this change:

| Portal | Live keyword test: `yeraltı suyu` |
| --- | --- |
| YÖK Tez | Native search succeeded; 1,027 reported matches, first 3 saved in the combined smoke test. A separate genuine zero-result search was also checked. |
| TR Dizin | Native API search succeeded; 484 reported matches, first 3 saved. A separate 25-record run verified pagination. |
| DergiPark | Both HTTP and Chromium reached the actual verification page. An article detail page was successfully parsed, but full search results after human verification were **not** verified here. |
| Sci-Hub | Live mirror access was previously blocked by this environment's network proxy. No real article PDF download has been verified; generated PDFs and simulated responses test the downloader. |

The combined live run saved six records and returned exit 1 because DergiPark was blocked. It did not label that run a complete three-portal search.

Offline regressions run with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use synthetic PDFs and simulated portal responses; they do not download real articles. Portal layouts and endpoints may change. The adapters fail explicitly when they do not recognize a response.

## Implementation references

Sci-Hub protocol patterns were reviewed in [DoiHive](https://github.com/SJRnhqh/DoiHive), [SciDownl](https://github.com/Tishacy/SciDownl), [scihub-cli](https://github.com/Oxidane-bot/scihub-cli), and [suqingdong's scihub](https://github.com/suqingdong/scihub). This is newly written code, not a bundled dependency on those projects.

Turkish adapters were checked against the live [YÖK Tez](https://tez.yok.gov.tr/UlusalTezMerkezi/), [TR Dizin](https://search.trdizin.gov.tr/), and [DergiPark](https://dergipark.org.tr/tr/search) portals. TR Dizin's public runtime configuration is read from `assets/env.json`. DergiPark result-card patterns were also checked against the public [browser scraper reference](https://github.com/saidsurucu/dergipark-skill/blob/main/scripts/lib.js); code was independently implemented.
