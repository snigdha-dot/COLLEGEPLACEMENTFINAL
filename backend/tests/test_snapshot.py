"""Tests for the CSV snapshot that carries the dataset between machines.

The property that matters: a snapshot must round-trip without losing or
corrupting anything, because it is the only copy of the data that lives in
git. The SQLite file is gitignored, so a lossy snapshot silently loses real
scraping spend.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _fresh_db(tmp: str, name: str) -> None:
    os.environ["DATABASE_PATH"] = str(Path(tmp) / name)


def test_snapshot_round_trips_without_loss() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_db(tmp, "a.db")
        from backend.db import repository as repo
        from backend.db.models import init_db
        from backend.db.session import get_conn
        from backend.snapshot import export_snapshot, restore_snapshot

        seeded = [
            {"college_name": "Complete College of Engineering", "state": "Karnataka",
             "stream": "Engineering", "district": "Bengaluru",
             "website": "https://complete.ac.in", "placement_email": "tpo@complete.ac.in",
             "placement_phone": "+91-9876543210",
             "backup_emails_found": ["info@complete.ac.in", "principal@complete.ac.in"],
             "backup_phones_found": ["+91-8012345678"],
             "source_urls": ["https://complete.ac.in/placement"],
             "confidence_score": 85, "email_verified": True, "status": "Verified"},
            {"college_name": "Half Filled Institute", "state": "Tamil Nadu",
             "stream": "BCA", "district": "Chennai",
             "fallback_contact_email": "info@half.ac.in", "status": "Needs Follow-up"},
        ]
        with get_conn() as conn:
            init_db(conn)
            for record in seeded:
                repo.upsert_college(conn, record)

        snapshot = Path(tmp) / "snap.csv"
        rows, visible = export_snapshot(snapshot)
        assert rows == 2
        assert visible == 1, "only the complete row is marketing-visible"

        # Restore into a completely separate database.
        _fresh_db(tmp, "b.db")
        restored, restored_visible = restore_snapshot(snapshot)
        assert restored == 2
        assert restored_visible == 1

        with get_conn() as conn:
            row = repo.admin_rows(conn, search="Complete College")[0]
            assert row["placement_email"] == "tpo@complete.ac.in"
            assert row["placement_phone"] == "+91-9876543210"
            assert row["confidence_score"] == 85
            assert row["email_verified"] == 1
            # Multi-value fields survive the comma-join round trip.
            assert "info@complete.ac.in" in row["backup_emails_found"]
            assert "principal@complete.ac.in" in row["backup_emails_found"]


def test_restore_never_overwrites_a_contact_with_a_blank() -> None:
    """Restoring an older snapshot must not erase newer local findings."""
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_db(tmp, "c.db")
        from backend.db import repository as repo
        from backend.db.models import init_db
        from backend.db.session import get_conn
        from backend.snapshot import export_snapshot, restore_snapshot

        with get_conn() as conn:
            init_db(conn)
            # An old snapshot: this college had no phone yet.
            repo.upsert_college(conn, {
                "college_name": "Growing College of Engineering", "state": "Karnataka",
                "stream": "Engineering", "district": "Mysuru",
                "placement_email": "tpo@growing.ac.in", "status": "Needs Follow-up",
            })
        old_snapshot = Path(tmp) / "old.csv"
        export_snapshot(old_snapshot)

        # Locally, a later scrape found the phone.
        with get_conn() as conn:
            college_id = repo.admin_rows(conn, search="Growing")[0]["id"]
            repo.update_college(conn, college_id,
                                {"placement_phone": "+91-9999888877"})

        # Restoring the OLD snapshot must not wipe that phone.
        restore_snapshot(old_snapshot)
        with get_conn() as conn:
            row = repo.admin_rows(conn, search="Growing")[0]
            assert row["placement_phone"] == "+91-9999888877", (
                "restoring an older snapshot erased a newer contact"
            )


def test_snapshot_excludes_the_surrogate_id() -> None:
    """Row ids are per-machine; matching is on the dedupe key instead."""
    from backend.snapshot import _EXPORT_COLUMNS

    assert "id" not in _EXPORT_COLUMNS
    assert "college_name" in _EXPORT_COLUMNS
    assert "placement_email" in _EXPORT_COLUMNS


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
