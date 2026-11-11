#!/usr/bin/env python3
"""Download a reviewed CSV through the integrated literature-search PDF flow."""

import argparse
import importlib.util
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-script", default=str(Path(__file__).with_name("search_articles7.py")),
                        help="Patched literature-search script (default: sibling search_articles7.py)")
    parser.add_argument("--from-csv", required=True)
    parser.add_argument("--mirror", "--scihub-mirror", action="append", default=None,
                        help="Sci-Hub HTTPS base URL; repeat for fallback mirrors")
    parser.add_argument("--pdf-dir", default="selected_pdfs")
    parser.add_argument("--out", default="selected_scihub.csv")
    parser.add_argument("--delay", type=float, default=2)
    parser.add_argument("--max-mb", type=float, default=100)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--enrich-abstracts", action="store_true")
    args = parser.parse_args(argv)

    script = Path(args.search_script).resolve()
    if not script.is_file():
        parser.error(f"Search script not found: {script}")
    output = Path(args.out).resolve()
    protected = [output, output.with_suffix(".audit.json")]
    protected += [Path(str(output) + suffix) for suffix in (
        ".responses.jsonl", ".metadata.csv", ".harvested.csv", ".all.csv")]
    if any(path.exists() for path in protected):
        parser.error("Output files already exist; choose a new --out")

    call = [str(script), "--from-csv", str(Path(args.from_csv).resolve()),
            "--download-pdfs", "--pdf-provider", "scihub", "--pdf-dir", args.pdf_dir,
            "--out", str(output), "--scihub-delay", str(args.delay),
            "--pdf-max-mb", str(args.max_mb), "--pdf-timeout", str(args.timeout)]
    for mirror in args.mirror or []:
        call.extend(["--scihub-mirror", mirror])
    if args.enrich_abstracts:
        call.append("--enrich-abstracts")

    module_name = "literature_selected_pdf_entrypoint"
    previous_path, previous_argv = sys.path[:], sys.argv[:]
    previous_module = sys.modules.get(module_name)
    try:
        sys.path.insert(0, str(script.parent))
        spec = importlib.util.spec_from_file_location(module_name, script)
        if spec is None or spec.loader is None:
            parser.error("Cannot load the search script")
        search = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = search
        spec.loader.exec_module(search)
        if not callable(getattr(search, "main", None)):
            parser.error("Search script has no main() function")
        sys.argv = call
        return search.main() or 0
    finally:
        sys.path[:] = previous_path
        sys.argv[:] = previous_argv
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module


if __name__ == "__main__":
    raise SystemExit(main())
