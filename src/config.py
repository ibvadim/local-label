"""Application paths, limits, and environment-based configuration."""

from __future__ import annotations

import os
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
LEGACY_ICONS_DIR = DATA_DIR / "icons"
LEGACY_STATE_FILE = DATA_DIR / "icons.json"
LEGACY_GROUPS_FILE = DATA_DIR / "asset_groups.json"
DATABASE_URL = os.getenv(
    "LOCALLABEL_DATABASE_URL", f"sqlite:///{DATA_DIR / 'locallabel.db'}"
)
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_BATCH_FILES = 200
MAX_BATCH_BYTES = 250 * 1024 * 1024
METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

DATA_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
