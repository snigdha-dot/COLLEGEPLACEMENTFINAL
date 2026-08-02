"""Read/write access to the colleges and scrape_runs tables.

Every query here uses `?` placeholders — no string-built SQL anywhere, per the
AGENTS.md security rule. The one place a value is interpolated is the ORDER BY
column, which is validated against an allow-list first.

Marketing reads go through `marketing_rows`, which is the only path that
applies the completeness filter and the internal/marketing projection defined
in db/models.py. Nothing else should build a marketing payload.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .models import (
    MARKETING_COMPLETENESS_FILTER,
    MARKETING_SELECT,
    STREAM_VALUES,
)
from ..scraper.normalize import dedupe_key

#: Columns a caller may sort by. Anything else is rejected rather than
#: interpolated, since ORDER BY cannot be parameterised.
SORTABLE_COLUMNS = frozenset({
    "college_name", "state", "stream", "district", "status",
    "outreach_status", "confidence_score", "last_scraped",
    # When the row first entered the DB. Distinct from last_scraped, which
    # changes every time a college is re-scraped — sorting by that would
    # reorder old colleges as they are refreshed. Marketing sorts newest-first
    # to see what has just been added.
    "created_at",
})

_UPSERT = """
INSERT INTO colleges (
    college_name, normalized_name, district, state, stream, affiliation, website,
    placement_officer_name, placement_email, placement_phone,
    backup_emails_found, backup_phones_found,
    fallback_contact_email, fallback_contact_phone,
    confidence_score, source_urls, email_verified, last_scraped, status
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (normalized_name, district, stream) DO UPDATE SET
    college_name           = excluded.college_name,
    affiliation            = COALESCE(NULLIF(excluded.affiliation, ''), affiliation),
    website                = COALESCE(NULLIF(excluded.website, ''), website),
    placement_officer_name = COALESCE(NULLIF(excluded.placement_officer_name, ''),
                                      placement_officer_name),
    placement_email        = COALESCE(NULLIF(excluded.placement_email, ''), placement_email),
    placement_phone        = COALESCE(NULLIF(excluded.placement_phone, ''), placement_phone),
    backup_emails_found    = excluded.backup_emails_found,
    backup_phones_found    = excluded.backup_phones_found,
    fallback_contact_email = COALESCE(NULLIF(excluded.fallback_contact_email, ''),
                                      fallback_contact_email),
    fallback_contact_phone = COALESCE(NULLIF(excluded.fallback_contact_phone, ''),
                                      fallback_contact_phone),
    confidence_score       = MAX(excluded.confidence_score, confidence_score),
    source_urls            = excluded.source_urls,
    email_verified         = excluded.email_verified,
    last_scraped           = excluded.last_scraped,
    status                 = excluded.status
-- outreach_status is deliberately NOT updated: it belongs to the marketing
-- team, and a re-scrape must never reset their work back to 'New'.
"""


def _join(values: Iterable[str] | None) -> str:
    return ", ".join(v for v in (values or []) if v)


def upsert_college(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Insert or update one college by (normalized_name, district, stream).

    Re-scraping preserves any contact already stored when the new run found
    nothing for that field, so a worse run never erases a better one.
    """
    stream = record.get("stream")
    if stream not in STREAM_VALUES:
        raise ValueError(f"invalid stream {stream!r}; expected one of {STREAM_VALUES}")

    normalized, district = dedupe_key(record["college_name"], record.get("district", ""))

    conn.execute(_UPSERT, (
        record["college_name"],
        normalized,
        district,
        record["state"],
        stream,
        record.get("affiliation", ""),
        record.get("website", ""),
        record.get("placement_officer_name", ""),
        record.get("placement_email", ""),
        record.get("placement_phone", ""),
        _join(record.get("backup_emails_found")),
        _join(record.get("backup_phones_found")),
        record.get("fallback_contact_email", ""),
        record.get("fallback_contact_phone", ""),
        int(record.get("confidence_score") or 0),
        _join(record.get("source_urls")),
        1 if record.get("email_verified") else 0,
        record.get("last_scraped", ""),
        record.get("status", "Needs Follow-up"),
    ))


def _filter_clause(
    state: str | None, stream: str | None, status: str | None,
    outreach_status: str | None, search: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if stream:
        clauses.append("stream = ?")
        params.append(stream)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if outreach_status:
        clauses.append("outreach_status = ?")
        params.append(outreach_status)
    if search:
        clauses.append("college_name LIKE ?")
        params.append(f"%{search}%")
    return (" AND ".join(clauses) if clauses else "1=1"), params


def _order_by(sort: str | None, direction: str) -> str:
    """Validate a sort column against the allow-list.

    ORDER BY cannot take a placeholder, so the only safe approach is to reject
    anything not explicitly permitted rather than escape it.
    """
    column = sort if sort in SORTABLE_COLUMNS else "college_name"
    return f"{column} {'DESC' if direction.lower() == 'desc' else 'ASC'}"


def admin_rows(
    conn: sqlite3.Connection, *, state: str | None = None, stream: str | None = None,
    status: str | None = None, outreach_status: str | None = None,
    search: str | None = None, sort: str | None = None, direction: str = "asc",
    limit: int = 500, offset: int = 0,
) -> list[sqlite3.Row]:
    """Full internal records — admin/QA view only. Never send this to marketing."""
    where, params = _filter_clause(state, stream, status, outreach_status, search)
    return conn.execute(
        f"SELECT * FROM colleges WHERE {where} "
        f"ORDER BY {_order_by(sort, direction)} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()


def marketing_rows(
    conn: sqlite3.Connection, *, state: str | None = None, stream: str | None = None,
    outreach_status: str | None = None, search: str | None = None,
    sort: str | None = None, direction: str = "asc",
    limit: int = 5000, offset: int = 0,
) -> list[sqlite3.Row]:
    """Marketing-visible rows: clean schema, complete rows only.

    The completeness filter (both email and phone required) is applied here,
    server-side, and cannot be bypassed by a caller — including the export
    endpoint, which must produce exactly this set.

    Note there is no `status` parameter: marketing does not filter by pipeline
    status because marketing never sees pipeline status.
    """
    where, params = _filter_clause(state, stream, None, outreach_status, search)
    return conn.execute(
        f"SELECT {MARKETING_SELECT} FROM colleges "
        f"WHERE {where} AND {MARKETING_COMPLETENESS_FILTER} "
        f"ORDER BY {_order_by(sort, direction)} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()


def get_college(conn: sqlite3.Connection, college_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM colleges WHERE id = ?", (college_id,)).fetchone()


#: Fields the detail view may edit by hand. Excludes everything the pipeline
#: owns (status, confidence, timestamps) and the dedupe key.
EDITABLE_COLUMNS = frozenset({
    "college_name", "affiliation", "website", "placement_officer_name",
    "placement_email", "placement_phone", "fallback_contact_email",
    "fallback_contact_phone", "outreach_status",
})


def update_college(
    conn: sqlite3.Connection, college_id: int, changes: dict[str, Any],
) -> int:
    """Apply a manual correction. Rejects any field outside EDITABLE_COLUMNS."""
    allowed = {k: v for k, v in changes.items() if k in EDITABLE_COLUMNS}
    if not allowed:
        return 0
    assignments = ", ".join(f"{column} = ?" for column in allowed)
    cursor = conn.execute(
        f"UPDATE colleges SET {assignments} WHERE id = ?",
        (*allowed.values(), college_id),
    )
    return cursor.rowcount


def counts_by_status(conn: sqlite3.Connection, state: str | None = None) -> dict[str, int]:
    where, params = ("state = ?", [state]) if state else ("1=1", [])
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS n FROM colleges WHERE {where} GROUP BY status",
        params,
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


# --- scrape runs -----------------------------------------------------------

def start_run(conn: sqlite3.Connection, state: str, stream: str, total: int) -> int:
    cursor = conn.execute(
        "INSERT INTO scrape_runs (state, stream, total_colleges) VALUES (?, ?, ?)",
        (state, stream, total),
    )
    return int(cursor.lastrowid or 0)


def update_run(
    conn: sqlite3.Connection, run_id: int, *, processed: int, succeeded: int, failed: int,
) -> None:
    conn.execute(
        "UPDATE scrape_runs SET processed = ?, succeeded = ?, failed = ? WHERE id = ?",
        (processed, succeeded, failed, run_id),
    )


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: str = "completed", notes: str = "",
) -> None:
    conn.execute(
        "UPDATE scrape_runs SET status = ?, notes = ?, finished_at = datetime('now') "
        "WHERE id = ?",
        (status, notes, run_id),
    )
