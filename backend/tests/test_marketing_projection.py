"""Guards on the marketing/internal boundary.

These rules are stated as absolutes in the brief ("never leave the internal
schema under any circumstance", "a row only appears if both email and phone are
non-empty"), so they get tests rather than trust. Run with:

    venv/Scripts/python.exe -m pytest backend/tests/ -q

pytest is a dev-only dependency and is deliberately NOT in requirements.txt
(that file is the runtime dependency set). Install it ad hoc if you want to run
these; the module also runs standalone via `python backend/tests/test_*.py`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.db.models import (  # noqa: E402
    INTERNAL_ONLY_COLUMNS,
    MARKETING_COLUMNS,
    MARKETING_COMPLETENESS_FILTER,
    MARKETING_SELECT,
    SCHEMA,
)

_INSERT = """
INSERT INTO colleges
    (college_name, normalized_name, district, state, stream,
     placement_email, placement_phone, fallback_contact_email, fallback_contact_phone,
     confidence_score, status, outreach_status, last_scraped)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# name, placement_email, placement_phone, fallback_email, fallback_phone, reaches_marketing
CASES = [
    ("Both placement",        "tpo@a.edu", "+91-1", "",           "",      True),
    ("Both fallback",         "",          "",      "info@b.edu", "+91-2", True),
    ("Placement + fallback",  "tpo@e.edu", "",      "",           "+91-5", True),
    ("Email only",            "tpo@c.edu", "",      "",           "",      False),
    ("Phone only",            "",          "+91-4", "",           "",      False),
    ("Nothing",               "",          "",      "",           "",      False),
    ("Nulls",                 None,        None,    None,         None,    False),
]


def _seeded_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for i, (name, pe, pp, fe, fp, _) in enumerate(CASES):
        conn.execute(
            _INSERT,
            (name, name.lower(), f"D{i}", "Karnataka", "Engineering",
             pe, pp, fe, fp, 95, "Verified", "Contacted", "2026-08-02T00:00:00"),
        )
    conn.commit()
    return conn


def _marketing_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {MARKETING_SELECT} FROM colleges "
        f"WHERE {MARKETING_COMPLETENESS_FILTER} ORDER BY college_name"
    ).fetchall()


def test_completeness_filter_requires_both_email_and_phone() -> None:
    """Both fields required; placement or fallback, doesn't matter which."""
    conn = _seeded_conn()
    visible = {r["college_name"] for r in _marketing_rows(conn)}
    expected = {name for name, *_, ok in CASES if ok}
    assert visible == expected, f"expected {expected}, got {visible}"


def test_empty_strings_count_as_missing() -> None:
    """'' must not satisfy the filter — NULLIF guards this."""
    conn = _seeded_conn()
    names = {r["college_name"] for r in _marketing_rows(conn)}
    assert "Email only" not in names
    assert "Phone only" not in names
    assert "Nothing" not in names
    assert "Nulls" not in names


def test_placement_contact_preferred_over_fallback() -> None:
    conn = _seeded_conn()
    rows = {r["college_name"]: r for r in _marketing_rows(conn)}
    assert rows["Both placement"]["email"] == "tpo@a.edu"
    # Placement email wins; phone falls back since placement_phone is empty.
    assert rows["Placement + fallback"]["email"] == "tpo@e.edu"
    assert rows["Placement + fallback"]["phone"] == "+91-5"


def test_internal_only_columns_never_reach_marketing() -> None:
    """status / last_scraped / confidence_score must not appear, ever."""
    conn = _seeded_conn()
    cols = set(_marketing_rows(conn)[0].keys())
    leaked = cols & INTERNAL_ONLY_COLUMNS
    assert not leaked, f"internal columns leaked to marketing: {leaked}"
    assert cols == set(MARKETING_COLUMNS)


def test_outreach_status_ships_blank() -> None:
    """Stored value is 'Contacted'; marketing must receive '' regardless."""
    conn = _seeded_conn()
    for row in _marketing_rows(conn):
        assert row["outreach_status"] == "", "pipeline must never pre-populate outreach_status"


def test_incomplete_rows_are_retained_internally() -> None:
    """Filtered-out rows stay in the DB for QA / re-scraping."""
    conn = _seeded_conn()
    total = conn.execute("SELECT COUNT(*) FROM colleges").fetchone()[0]
    assert total == len(CASES)
    assert len(_marketing_rows(conn)) < total


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
