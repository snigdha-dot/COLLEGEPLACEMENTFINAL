"""Tests for DB access: upsert semantics, the marketing boundary, and SQL safety.

The upsert rules matter because re-scraping is routine: a second run that finds
less than the first must not erase what the first found, and must never reset
marketing's own outreach_status.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.db.models import INTERNAL_ONLY_COLUMNS, MARKETING_COLUMNS, SCHEMA  # noqa: E402
from backend.db import repository as repo  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _record(name: str = "BMS College of Engineering", **overrides) -> dict:
    base = {
        "college_name": name,
        "state": "Karnataka",
        "stream": "Engineering",
        "district": "Bengaluru",
        "website": "https://bmsce.ac.in",
        "placement_email": "placement@bmsce.ac.in",
        "placement_phone": "+91-9876543210",
        "backup_emails_found": ["info@bmsce.ac.in"],
        "backup_phones_found": ["+91-8012345678"],
        "confidence_score": 85,
        "source_urls": ["https://bmsce.ac.in/placement"],
        "email_verified": True,
        "last_scraped": "2026-08-02T10:00:00",
        "status": "Verified",
    }
    base.update(overrides)
    return base


def test_upsert_inserts_then_updates_same_college() -> None:
    conn = _conn()
    repo.upsert_college(conn, _record())
    # Same college, different spelling — the dedupe key must match.
    repo.upsert_college(conn, _record("B.M.S. College of Engineering, Bangalore"))
    assert conn.execute("SELECT COUNT(*) FROM colleges").fetchone()[0] == 1


def test_rescrape_does_not_erase_existing_contacts() -> None:
    """A later run that finds nothing must not blank a good earlier result."""
    conn = _conn()
    repo.upsert_college(conn, _record())
    repo.upsert_college(conn, _record(
        placement_email="", placement_phone="", status="Needs Follow-up",
    ))
    row = conn.execute("SELECT * FROM colleges").fetchone()
    assert row["placement_email"] == "placement@bmsce.ac.in"
    assert row["placement_phone"] == "+91-9876543210"


def test_rescrape_never_resets_outreach_status() -> None:
    """outreach_status belongs to marketing; a re-scrape must not touch it."""
    conn = _conn()
    repo.upsert_college(conn, _record())
    conn.execute("UPDATE colleges SET outreach_status = 'Contacted'")
    repo.upsert_college(conn, _record())
    assert conn.execute("SELECT outreach_status FROM colleges").fetchone()[0] == "Contacted"


def test_confidence_score_only_climbs() -> None:
    conn = _conn()
    repo.upsert_college(conn, _record(confidence_score=85))
    repo.upsert_college(conn, _record(confidence_score=30))
    assert conn.execute("SELECT confidence_score FROM colleges").fetchone()[0] == 85


def test_invalid_stream_rejected() -> None:
    conn = _conn()
    try:
        repo.upsert_college(conn, _record(stream="Medicine"))
    except ValueError:
        pass
    else:
        raise AssertionError("invalid stream should raise")


def test_marketing_rows_apply_completeness_filter() -> None:
    conn = _conn()
    repo.upsert_college(conn, _record("Complete College of Engineering"))
    repo.upsert_college(conn, _record(
        "Email Only Institute", placement_phone="", fallback_contact_phone="",
        district="Mysuru",
    ))
    rows = repo.marketing_rows(conn)
    names = [r["college_name"] for r in rows]
    assert "Complete College of Engineering" in names
    assert "Email Only Institute" not in names


def test_marketing_rows_never_expose_internal_columns() -> None:
    conn = _conn()
    repo.upsert_college(conn, _record())
    row = repo.marketing_rows(conn)[0]
    assert set(row.keys()) == set(MARKETING_COLUMNS)
    assert not set(row.keys()) & INTERNAL_ONLY_COLUMNS


def test_marketing_rows_ship_blank_outreach_status() -> None:
    conn = _conn()
    repo.upsert_college(conn, _record())
    conn.execute("UPDATE colleges SET outreach_status = 'Responded'")
    assert repo.marketing_rows(conn)[0]["outreach_status"] == ""


def test_admin_rows_keep_full_schema() -> None:
    conn = _conn()
    repo.upsert_college(conn, _record())
    row = repo.admin_rows(conn)[0]
    for column in ("status", "confidence_score", "last_scraped"):
        assert column in row.keys(), f"admin view lost {column}"


def test_sort_column_allow_list_blocks_injection() -> None:
    """ORDER BY cannot be parameterised, so anything unknown must be dropped."""
    conn = _conn()
    repo.upsert_college(conn, _record())
    # Would be catastrophic if interpolated; must fall back to the default.
    rows = repo.admin_rows(conn, sort="college_name; DROP TABLE colleges--")
    assert len(rows) == 1
    assert conn.execute("SELECT COUNT(*) FROM colleges").fetchone()[0] == 1


def test_search_filter_is_parameterised() -> None:
    conn = _conn()
    repo.upsert_college(conn, _record())
    assert repo.admin_rows(conn, search="'; DROP TABLE colleges--") == []
    assert conn.execute("SELECT COUNT(*) FROM colleges").fetchone()[0] == 1


def test_update_college_rejects_non_editable_fields() -> None:
    """The pipeline owns status/confidence; a hand edit must not set them."""
    conn = _conn()
    repo.upsert_college(conn, _record())
    college_id = conn.execute("SELECT id FROM colleges").fetchone()["id"]

    repo.update_college(conn, college_id, {
        "placement_email": "corrected@bmsce.ac.in",   # allowed
        "status": "Verified",                          # not allowed
        "confidence_score": 100,                       # not allowed
    })
    row = repo.get_college(conn, college_id)
    assert row["placement_email"] == "corrected@bmsce.ac.in"
    assert row["confidence_score"] == 85, "confidence_score should be unchanged"


def test_counts_by_status() -> None:
    conn = _conn()
    repo.upsert_college(conn, _record("A College of Engineering", status="Verified"))
    repo.upsert_college(conn, _record("B College of Engineering", status="Failed",
                                      district="Mysuru"))
    counts = repo.counts_by_status(conn, state="Karnataka")
    assert counts.get("Verified") == 1
    assert counts.get("Failed") == 1


def test_run_lifecycle() -> None:
    conn = _conn()
    run_id = repo.start_run(conn, "Karnataka", "Engineering", total=84)
    repo.update_run(conn, run_id, processed=10, succeeded=8, failed=2)
    repo.finish_run(conn, run_id, "completed", "pilot run")
    row = conn.execute("SELECT * FROM scrape_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["processed"] == 10 and row["status"] == "completed"
    assert row["finished_at"] is not None


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
