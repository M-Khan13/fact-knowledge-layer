"""Ingest one or more PDFs into a collection.

    python scripts/ingest.py --collection macro --input ~/some/folder
    python scripts/ingest.py --collection macro --pdf a.pdf --pdf b.pdf

Documents are ingested one at a time and skipped if the collection already
holds them, so adding a document never reprocesses the rest.

Needs GEMINI_API_KEY in .env. Use --max-pages while trying things out; a full
100-page report is one model call per page.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, store  # noqa: E402
from backend.pipeline.ingest import ingest_pdf  # noqa: E402


def collect_pdfs(pdfs: list[str] | None, input_dir: str | None) -> list[Path]:
    """Resolve which PDFs to ingest, from explicit paths or a directory."""
    if pdfs:
        paths = [Path(p).expanduser() for p in pdfs]
        missing = [p for p in paths if not p.is_file()]
        if missing:
            raise SystemExit(f"Not a file: {', '.join(str(m) for m in missing)}")
        return paths

    directory = config.resolve_input_dir(input_dir)
    found = sorted(directory.glob("*.pdf"))
    if not found:
        raise SystemExit(f"No PDFs in {directory}. Pass --pdf, or set INPUT_DIR / --input.")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", required=True, help="collection to ingest into")
    parser.add_argument("--pdf", action="append", help="a PDF to ingest; repeatable")
    parser.add_argument("--input", help="directory of PDFs; overrides INPUT_DIR")
    parser.add_argument("--db", help="database path; overrides DATABASE_PATH")
    parser.add_argument("--model", help="extraction model; overrides GEMINI_EXTRACTION_MODEL")
    parser.add_argument("--max-pages", type=int, help="only read the first N pages")
    parser.add_argument(
        "--force", action="store_true", help="re-ingest documents already stored"
    )
    args = parser.parse_args(argv)

    paths = collect_pdfs(args.pdf, args.input)

    with store.session(args.db) as conn:
        store.upsert_collection(conn, args.collection)
        total = 0

        for path in paths:
            try:
                report = ingest_pdf(
                    path,
                    args.collection,
                    conn,
                    model=args.model,
                    max_pages=args.max_pages,
                    skip_if_present=not args.force,
                )
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from None
            print(report.summary())
            if report.failed_pages:
                print(f"    pages that would not parse: {report.failed_pages}")
            total += report.grounded

        print(
            f"\n{total} facts added; collection '{args.collection}' now holds "
            f"{store.count_facts(conn, args.collection)}."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
