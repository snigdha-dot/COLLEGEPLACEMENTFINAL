"""Tests for the Excel/CSV export.

The rule under test is absolute in the brief: an exported file must never
contain status, last_scraped, or confidence_score, and must never contain a row
missing either an email or a phone — regardless of which view triggered it.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.api.export import (  # noqa: E402
    COLUMN_LABELS,
    build_export_frame,
    export_filename,
    to_csv_bytes,
    to_excel_bytes,
)
from backend.db import repository as repo  # noqa: E402
from backend.db.models import SCHEMA  # noqa: E402

FORBIDDEN_HEADERS = {
    "status", "last_scraped", "confidence_score", "Status", "Last Scraped",
    "Confidence Score", "id", "source_urls", "email_verified",
}


def _conn_with_rows() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    repo.upsert_college(conn, {
        "college_name": "Complete College of Engineering", "state": "Karnataka",
        "stream": "Engineering", "district": "Bengaluru",
        "placement_email": "tpo@complete.ac.in", "placement_phone": "+91-9876543210",
        "backup_emails_found": ["info@complete.ac.in"],
        "backup_phones_found": ["+91-8012345678"],
        "confidence_score": 90, "status": "Verified",
        "last_scraped": "2026-08-02T10:00:00", "email_verified": True,
    })
    repo.upsert_college(conn, {
        "college_name": "Fallback Only Institute", "state": "Karnataka",
        "stream": "Engineering", "district": "Mysuru",
        "fallback_contact_email": "info@fallback.ac.in",
        "fallback_contact_phone": "+91-9812345678",
        "confidence_score": 25, "status": "Needs Follow-up",
    })
    repo.upsert_college(conn, {
        "college_name": "Email Only College of Engineering", "state": "Karnataka",
        "stream": "Engineering", "district": "Hassan",
        "placement_email": "tpo@emailonly.ac.in",
        "confidence_score": 70, "status": "Needs Follow-up",
    })
    repo.upsert_college(conn, {
        "college_name": "BCA Complete College", "state": "Karnataka",
        "stream": "BCA", "district": "Bengaluru",
        "placement_email": "tpo@bca.ac.in", "placement_phone": "+91-9700000000",
        "status": "Verified",
    })
    return conn


def test_export_excludes_incomplete_rows() -> None:
    frame = build_export_frame(_conn_with_rows())
    names = set(frame["College Name"])
    assert "Complete College of Engineering" in names
    assert "Fallback Only Institute" in names, "fallback contacts count as complete"
    assert "Email Only College of Engineering" not in names, "missing phone must be excluded"


def test_export_never_contains_internal_columns() -> None:
    frame = build_export_frame(_conn_with_rows())
    assert not set(frame.columns) & FORBIDDEN_HEADERS, (
        f"internal columns leaked: {set(frame.columns) & FORBIDDEN_HEADERS}"
    )


def test_export_columns_are_human_readable() -> None:
    frame = build_export_frame(_conn_with_rows())
    assert list(frame.columns) == list(COLUMN_LABELS.values())


def test_export_outreach_status_is_blank() -> None:
    """Marketing fills this in; the pipeline must never pre-populate it."""
    conn = _conn_with_rows()
    conn.execute("UPDATE colleges SET outreach_status = 'Contacted'")
    frame = build_export_frame(conn)
    assert (frame["Outreach Status"] == "").all()


def test_export_respects_stream_filter() -> None:
    frame = build_export_frame(_conn_with_rows(), stream="BCA")
    assert set(frame["Stream"]) == {"BCA"}


def test_export_respects_search_filter() -> None:
    """Substring match, so "Complete College" hits both the Engineering and
    the BCA row; "of Engineering" narrows it to one."""
    both = build_export_frame(_conn_with_rows(), search="Complete College")
    assert len(both) == 2

    one = build_export_frame(_conn_with_rows(), search="Complete College of Engineering")
    assert len(one) == 1
    assert one.iloc[0]["College Name"] == "Complete College of Engineering"


def test_excel_bytes_roundtrip() -> None:
    frame = build_export_frame(_conn_with_rows())
    data = to_excel_bytes(frame)
    assert data[:2] == b"PK", "not a valid xlsx (zip) file"

    reloaded = pd.read_excel(io.BytesIO(data))
    assert list(reloaded.columns) == list(frame.columns)
    assert len(reloaded) == len(frame)
    assert not set(reloaded.columns) & FORBIDDEN_HEADERS


def test_csv_bytes_roundtrip() -> None:
    frame = build_export_frame(_conn_with_rows())
    data = to_csv_bytes(frame)
    assert data.startswith(b"\xef\xbb\xbf"), "missing utf-8 BOM; Excel will mangle names"

    reloaded = pd.read_csv(io.BytesIO(data))
    assert list(reloaded.columns) == list(frame.columns)
    assert not set(reloaded.columns) & FORBIDDEN_HEADERS


def test_empty_export_is_valid() -> None:
    """No matching rows must still produce a well-formed file with headers."""
    conn = _conn_with_rows()
    frame = build_export_frame(conn, state="Nowhere")
    assert len(frame) == 0
    assert list(frame.columns) == list(COLUMN_LABELS.values())
    assert to_excel_bytes(frame)[:2] == b"PK"
    reloaded = pd.read_csv(io.BytesIO(to_csv_bytes(frame)))
    assert list(reloaded.columns) == list(frame.columns)


def test_export_filename_shape() -> None:
    name = export_filename("Karnataka", "Engineering", "xlsx")
    assert name.startswith("college_contacts_karnataka_engineering_")
    assert name.endswith(".xlsx")
    assert export_filename(None, None, "csv").endswith(".csv")


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
