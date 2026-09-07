"""Configuration loaded from the environment.

Secrets are read from a .env file via python-dotenv and are never logged or
returned by any endpoint. Only the presence of a key is ever exposed.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")


def _path_setting(name: str, default: str) -> Path:
    """Resolve a path setting relative to the repo root unless it is absolute."""
    raw = os.getenv(name, default)
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_EXTRACTION_MODEL = os.getenv("GEMINI_EXTRACTION_MODEL", "gemini-2.5-flash")
GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")

DATABASE_PATH = _path_setting("DATABASE_PATH", "data/facts.db")
UPLOAD_DIR = _path_setting("UPLOAD_DIR", "data/uploads")

# Where source PDFs are read from when a script is not given an explicit path.
# Always configured, never hardcoded to a machine-specific location.
INPUT_DIR = _path_setting("INPUT_DIR", "data/input")


# Whether an evidence span must match the page character for character. A PDF's
# line breaks fall where a model does not reliably reproduce them, so the
# relaxed setting forgives whitespace only - every word and digit must still be
# present, in order, on that page.
REQUIRE_EXACT_SPANS = os.getenv("REQUIRE_EXACT_SPANS", "true").strip().lower() in {
    "1", "true", "yes", "on",
}


# Thinking tokens are billed as output and dominated the cost of a trial run,
# so the budget is capped. Extraction is a mechanical copying task rather than
# a reasoning one. Set to -1 to let the model decide.
THINKING_BUDGET = int(os.getenv("THINKING_BUDGET", "512"))

# Seconds to leave between model calls, to stay inside a per-minute quota.
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "6.0"))

# A fact carrying a unit is a measurement, so its value must contain a digit.
# Values without a unit are left alone, so a status or a role still gets through.
REQUIRE_DIGIT_WITH_UNIT = os.getenv("REQUIRE_DIGIT_WITH_UNIT", "true").strip().lower() in {
    "1", "true", "yes", "on",
}


def has_gemini_key() -> bool:
    """True when an API key is configured. Never exposes the key itself."""
    return bool(GEMINI_API_KEY.strip())


def resolve_input_dir(override: str | None = None) -> Path:
    """Pick the PDF input directory: an explicit argument wins over INPUT_DIR.

    Lets a caller pass --input on the command line while still defaulting to
    the configured location, so no source path is baked into the code.
    """
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else REPO_ROOT / path
    return INPUT_DIR
