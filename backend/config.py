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
GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "text-embedding-004")

DATABASE_PATH = _path_setting("DATABASE_PATH", "data/facts.db")
UPLOAD_DIR = _path_setting("UPLOAD_DIR", "data/uploads")


def has_gemini_key() -> bool:
    """True when an API key is configured. Never exposes the key itself."""
    return bool(GEMINI_API_KEY.strip())
