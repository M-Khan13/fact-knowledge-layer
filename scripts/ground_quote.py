"""Locate a quote in a PDF and optionally save the highlighted page.

A hand tool for checking grounding against any document:

    python scripts/ground_quote.py --pdf path/to.pdf --quote "some text" --out hit.png

With no --pdf, the first PDF in the configured input directory is used.
The directory comes from --input, else INPUT_DIR in .env - never from a path
baked into the code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Run directly (`python scripts/ground_quote.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from backend.pipeline.grounding import ground, render_grounding  # noqa: E402
from backend.pipeline.parsing import parse_pdf  # noqa: E402


def pick_pdf(pdf: str | None, input_dir: str | None) -> Path:
    """Resolve which PDF to read, preferring an explicit --pdf."""
    if pdf:
        return Path(pdf).expanduser()

    directory = config.resolve_input_dir(input_dir)
    candidates = sorted(directory.glob("*.pdf"))
    if not candidates:
        raise SystemExit(
            f"No PDFs in {directory}. Pass --pdf, or set INPUT_DIR / --input."
        )
    return candidates[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quote", required=True, help="text to locate")
    parser.add_argument("--pdf", help="PDF to search; defaults to first in input dir")
    parser.add_argument("--input", help="directory of PDFs; overrides INPUT_DIR")
    parser.add_argument("--out", help="write the highlighted page here as PNG")
    parser.add_argument("--min-score", type=float, default=None)
    args = parser.parse_args(argv)

    path = pick_pdf(args.pdf, args.input)
    kwargs = {} if args.min_score is None else {"min_score": args.min_score}

    with parse_pdf(path) as doc:
        print(f"document : {doc.source_doc} ({doc.page_count} pages)")
        result = ground(args.quote, doc, **kwargs)

        if result is None:
            print("not grounded: that text was not found in this document")
            return 1

        label = f" (label {result.page_label})" if result.page_label else ""
        print(f"page     : {result.page_number}{label}")
        print(f"method   : {result.method}  score={result.score:.3f}")
        print(f"bbox     : {tuple(round(v, 1) for v in result.bbox)}")
        print(f"rects    : {len(result.rects)}")
        print(f"matched  : {result.matched_text[:200]}")

        if args.out:
            out = Path(args.out).expanduser()
            out.write_bytes(render_grounding(doc, result))
            print(f"written  : {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
