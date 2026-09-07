"""Score extraction against a hand-labelled fact set.

    python scripts/evaluate.py --labels tests/fixtures/eval-labels.json \
                               --input ~/path/to/pdfs

Reads the labels, ingests whichever PDFs they name, and reports precision,
recall and every miss. Labels come only from the file; nothing here invents
one, and a missing file is an error rather than an empty run.

Inspect the file first if you are unsure it will be read correctly:

    python scripts/evaluate.py --labels <file> --describe
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, evaluation, store  # noqa: E402
from backend.pipeline.ingest import ingest_pdf, parse_page_ranges  # noqa: E402


def find_documents(labels, input_dir: Path) -> tuple[list[Path], list[str]]:
    """Match the documents the labels name to PDFs on disk.

    Names are matched on filename stem, so a label may name the file with or
    without its extension.
    """
    # Searched recursively, so one run can cover datasets kept in subfolders.
    available = {path.stem.lower(): path for path in sorted(input_dir.rglob("*.pdf"))}
    wanted = sorted({label.document for label in labels if label.document})

    if not wanted:
        # Labels name no document; evaluate against everything available.
        return list(available.values()), []

    found, missing = [], []
    for name in wanted:
        path = available.get(Path(name).stem.lower())
        if path is None:
            missing.append(name)
        else:
            found.append(path)
    return found, missing


def pages_for_document(labels, path: Path, pad: int = 0) -> set[int]:
    """The pages this document's labels cite, optionally with neighbours.

    Lets a run read only where the labelled facts actually are, which is the
    difference between a handful of calls and a whole corpus.
    """
    stem = path.stem.lower()
    pages: set[int] = set()
    for label in labels:
        if label.document and Path(label.document).stem.lower() != stem:
            continue
        for page in label.pages:
            pages.update(range(max(1, page - pad), page + pad + 1))
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labels",
        default="tests/fixtures/eval-labels.json",
        help="JSON file of hand-labelled facts",
    )
    parser.add_argument("--input", help="directory of PDFs; overrides INPUT_DIR")
    parser.add_argument("--collection", default="eval", help="collection to build")
    parser.add_argument("--db", help="database path; overrides DATABASE_PATH")
    parser.add_argument("--model", help="extraction model")
    parser.add_argument("--max-pages", type=int, help="only read the first N pages")
    parser.add_argument(
        "--pages", help="only read these 1-based pages, e.g. '3-5,10,14'"
    )
    parser.add_argument(
        "--label-pages",
        action="store_true",
        help="read only the pages the labels cite, per document",
    )
    parser.add_argument(
        "--pad-pages",
        type=int,
        default=0,
        help="with --label-pages, also read this many pages either side",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="show how the label file was read, then stop",
    )
    parser.add_argument(
        "--no-ingest",
        action="store_true",
        help="score the facts already in the collection, without re-extracting",
    )
    parser.add_argument("--json", dest="json_out", help="write results to this file")
    parser.add_argument("--show", type=int, default=30, help="rows per list in the report")
    args = parser.parse_args(argv)

    try:
        loaded = evaluation.load_labels(args.labels)
    except (FileNotFoundError, evaluation.LabelFormatError) as exc:
        raise SystemExit(str(exc)) from None

    print(loaded.describe())
    if args.describe:
        return 0

    with store.session(args.db) as conn:
        store.upsert_collection(conn, args.collection)

        if not args.no_ingest:
            input_dir = config.resolve_input_dir(args.input)
            if not input_dir.is_dir():
                raise SystemExit(
                    f"No such directory: {input_dir}. Pass --input, or set INPUT_DIR."
                )

            documents, missing = find_documents(loaded.labels, input_dir)
            if missing:
                print(f"\nLabelled documents not found in {input_dir}:")
                for name in missing:
                    print(f"    {name}")
            if not documents:
                raise SystemExit(
                    f"None of the labelled documents were found in {input_dir}."
                )

            try:
                explicit_pages = parse_page_ranges(args.pages)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None

            print(f"\nIngesting {len(documents)} document(s) into '{args.collection}':")
            for path in documents:
                wanted = explicit_pages
                if args.label_pages:
                    wanted = pages_for_document(loaded.labels, path, args.pad_pages)
                    if not wanted:
                        print(f"    {path.stem}: no pages cited, skipped")
                        continue
                    print(f"    {path.stem}: pages {sorted(wanted)}")
                try:
                    report = ingest_pdf(
                        path,
                        args.collection,
                        conn,
                        model=args.model,
                        max_pages=args.max_pages,
                        pages=wanted,
                    )
                except RuntimeError as exc:
                    raise SystemExit(str(exc)) from None
                print(f"    {report.summary()}")

        facts = store.list_facts(conn, args.collection)

    if not facts:
        raise SystemExit(
            f"No facts in collection '{args.collection}'. Ingest first, or drop "
            "--no-ingest."
        )

    result = evaluation.evaluate(loaded.labels, facts)
    print(evaluation.format_report(result, show=args.show))

    if args.json_out:
        out = Path(args.json_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(evaluation.result_to_dict(result), indent=2), encoding="utf-8"
        )
        print(f"Results written to {out}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
