#!/usr/bin/env python3
"""Standalone DOI/CSV PDF downloader; shared implementation lives in scihub_downloader."""
from scihub_downloader import (  # Re-export the useful original import surface.
    DEFAULT_MIRRORS, PDF_FIELDS, SCIHUB_BASES, SciHubDownloader,
    download_rows, download_scihub_pdf, main, normalize_doi,
    read_selected, safe_pdf_filename, session_with_scihub_headers,
)


if __name__ == "__main__":
    raise SystemExit(main())
