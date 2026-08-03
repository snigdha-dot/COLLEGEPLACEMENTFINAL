"""Move the college dataset between machines as a versioned CSV.

The SQLite file itself stays gitignored (AGENTS.md): a binary blob conflicts
on every concurrent edit and cannot be reviewed in a diff. But the data in it
represents real scraping spend, so it should not be trapped on one laptop.

This exports the full internal schema to `data/colleges_snapshot.csv`, which
IS committed, and rebuilds the DB from that file on the other machine. The CSV
is diffable, so a pull shows exactly which colleges and contacts changed.

SENSITIVE: the snapshot contains contact PII — hundreds of real email
addresses and phone numbers. AGENTS.md requires this be treated as sensitive
data, so the repository holding it MUST stay private. Do not publish it, and
do not attach the file to a ticket or a chat thread.

    python -m backend.snapshot export     # DB  -> data/colleges_snapshot.csv
    python -m backend.snapshot restore    # CSV -> DB (merges, never deletes)
    python -m backend.snapshot status     # compare the two

Restore uses the same upsert as the scraper: existing rows are updated field
by field and a blank incoming value never overwrites stored data, so restoring
an older snapshot cannot destroy newer local findings.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from .db.models import INTERNAL_COLUMNS, init_db
from .db.repository import marketing_rows, upsert_college
from .db.session import get_conn

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_FILE = SNAPSHOT_DIR / "colleges_snapshot.csv"

#: Everything except the surrogate id, which is meaningless across machines —
#: rows are matched on the (normalized_name, district, stream) key instead.
_EXPORT_COLUMNS = [c for c in INTERNAL_COLUMNS if c != "id"]


def export_snapshot(path: Path = SNAPSHOT_FILE) -> tuple[int, int]:
    """Write every DB row to CSV. Returns (rows, marketing_visible)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_EXPORT_COLUMNS)} FROM colleges ORDER BY college_name"
        ).fetchall()
        visible = len(marketing_rows(conn, limit=1_000_000))

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: (row[c] if row[c] is not None else "") for c in _EXPORT_COLUMNS})

    return len(rows), visible


def restore_snapshot(path: Path = SNAPSHOT_FILE) -> tuple[int, int]:
    """Rebuild the DB from CSV. Returns (rows_read, marketing_visible)."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `python -m backend.snapshot export` on the "
            f"machine that has the data, and commit the result."
        )

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    with get_conn() as conn:
        init_db(conn)
        for row in rows:
            upsert_college(conn, {
                **row,
                # These arrive as comma-joined strings; upsert_college re-joins
                # whatever it is given, so splitting keeps the round trip exact.
                "backup_emails_found": [
                    v.strip() for v in (row.get("backup_emails_found") or "").split(",") if v.strip()
                ],
                "backup_phones_found": [
                    v.strip() for v in (row.get("backup_phones_found") or "").split(",") if v.strip()
                ],
                "source_urls": [
                    v.strip() for v in (row.get("source_urls") or "").split(",") if v.strip()
                ],
                "confidence_score": int(row.get("confidence_score") or 0),
                "email_verified": str(row.get("email_verified", "")).strip() in {"1", "True", "true"},
            })

    with get_conn() as conn:
        visible = len(marketing_rows(conn, limit=1_000_000))
    return len(rows), visible


def status(path: Path = SNAPSHOT_FILE) -> None:
    with get_conn() as conn:
        db_rows = conn.execute("SELECT COUNT(*) FROM colleges").fetchone()[0]
        db_visible = len(marketing_rows(conn, limit=1_000_000))

    print(f"database : {db_rows} rows, {db_visible} marketing-visible")
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            snapshot_rows = sum(1 for _ in csv.DictReader(handle))
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        print(f"snapshot : {snapshot_rows} rows  ({path.name}, {stamp:%Y-%m-%d %H:%M} UTC)")
        if snapshot_rows != db_rows:
            print(f"\nThey differ by {abs(snapshot_rows - db_rows)} rows — run "
                  f"`export` to update the snapshot, or `restore` to load it.")
    else:
        print(f"snapshot : none at {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "restore", "status"])
    parser.add_argument("--file", type=Path, default=SNAPSHOT_FILE)
    args = parser.parse_args(argv)

    if args.action == "export":
        rows, visible = export_snapshot(args.file)
        print(f"exported {rows} rows ({visible} marketing-visible) -> {args.file}")
        print("Commit this file so the data travels with the repo.")
    elif args.action == "restore":
        try:
            rows, visible = restore_snapshot(args.file)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"restored {rows} rows -> {visible} marketing-visible")
    else:
        status(args.file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
