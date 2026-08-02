"""Shared FastAPI dependencies: DB connections and rate limiting."""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict, deque
from typing import Iterator

from fastapi import HTTPException, Request

from ..db.models import init_db
from ..db.session import connect


def get_connection() -> Iterator[sqlite3.Connection]:
    """Per-request SQLite connection, committed on success."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    """Create tables if absent. Called once at startup."""
    conn = connect()
    try:
        init_db(conn)
        conn.commit()
    finally:
        conn.close()


class RateLimiter:
    """Fixed-window limiter, per client IP.

    AGENTS.md requires rate limiting on any endpoint that triggers a scrape or
    seed build, because each one spends real Ollagraph credits — an accidental
    double-click on "Re-scrape" should not cost twice.

    In-process and therefore per-worker: adequate for an internal tool on a
    single instance, and deliberately not a distributed limiter, which would
    mean adding Redis for a tool with a handful of users.
    """

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self.max_calls = max_calls
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def __call__(self, request: Request) -> None:
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        hits = self._hits[key]

        while hits and now - hits[0] > self.window:
            hits.popleft()

        if len(hits) >= self.max_calls:
            retry_after = int(self.window - (now - hits[0])) + 1
            raise HTTPException(
                429,
                detail=(
                    f"rate limit reached ({self.max_calls} per "
                    f"{int(self.window)}s). Retry in {retry_after}s."
                ),
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)


#: Scrape and seed-build jobs are expensive in both time and credits.
scrape_rate_limit = RateLimiter(max_calls=5, window_seconds=60)
seed_rate_limit = RateLimiter(max_calls=3, window_seconds=300)
#: Exports only read the DB, so the limit exists to stop runaway clients.
export_rate_limit = RateLimiter(max_calls=20, window_seconds=60)
