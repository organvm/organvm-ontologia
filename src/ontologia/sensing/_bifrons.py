"""Shared read-only access to the BIFRONS portal store for sensors.

Sensors observe the portal store written by alchemia; they never write to it.
Ontologia cannot import the alchemia package, so the DB path resolution is
mirrored here (kept trivially in sync via ``$BIFRONS_DB``).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def portal_db_path() -> Path:
    """Resolve the BIFRONS portal DB path (``$BIFRONS_DB`` overrides default)."""
    env = os.environ.get("BIFRONS_DB")
    if env:
        return Path(env).expanduser()
    return Path("~/.organvm/bifrons/portal.db").expanduser()


def open_readonly(path: Path) -> sqlite3.Connection | None:
    """Open the portal DB read-only, or return None if it does not exist."""
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None
