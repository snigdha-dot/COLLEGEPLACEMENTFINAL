"""SQLite connection management.

Raw stdlib sqlite3, no ORM — see context.md for why. Every query in this
project uses `?` placeholders; never build SQL by string concatenation or
f-string (AGENTS.md security rule).
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

load_dotenv()

DEFAULT_DB_PATH = "./colleges.db"


def get_db_path() -> Path:
    """Resolve the SQLite file location from env, falling back to the default."""
    return Path(os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)).resolve()


def connect() -> sqlite3.Connection:
    """Open a connection with the pragmas this project relies on.

    WAL matters here: the scraper writes rows as it goes while the API may be
    reading concurrently, and a scrape run that dies partway must leave the
    rows it already committed intact (the brief calls for resumability).
    """
    db_path = get_db_path()
    # sqlite3 raises a bare "unable to open database file" when the parent
    # directory is missing, which reads like a permissions problem. Create it.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Connection context manager: commits on success, rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
