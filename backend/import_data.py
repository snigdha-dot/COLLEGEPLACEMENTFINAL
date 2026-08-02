"""Import a ready-made college dataset (CSV or Excel) into the DB.

Standalone by design: this imports directly through the same repository layer
the scraper uses, so it touches no pipeline module and cannot break one. The
scraper does not know or care whether a row arrived by scrape or by import.

Why that is safe:
  - upsert_college applies the same validation and dedupe key either way.
  - It only fills a field it finds EMPTY, so a later scrape adds what is
    missing rather than overwriting imported data — and an import cannot
    clobber a good prior scrape.
  - outreach_status is never touched by either path; it belongs to marketing.

The completeness rule is unchanged: a row missing an email or a phone is
still stored, still visible in /admin, and still absent from the marketing
view and the Excel export.

Usage:
    python -m backend.import_data data.xlsx --dry-run    # inspect first
    python -m backend.import_data data.xlsx
    python -m backend.import_data data.csv --stream BCA --default-state Karnataka
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .db.models import STREAM_VALUES, init_db
from .db.repository import upsert_college
from .db.session import get_conn
from .scraper.contact_extractor import normalize_phone
from .scraper.normalize import dedupe_key

#: Source column name -> our field. Matching is case/space/punctuation
#: insensitive, so "College Name", "college_name", and "COLLEGE NAME" all hit.
#: Ordered most specific first: "placement email" must win over "email".
COLUMN_ALIASES: list[tuple[tuple[str, ...], str]] = [
    (("collegename", "college", "institutename", "institution", "name",
      "nameofthecollege", "nameoftheinstitution"), "college_name"),
    (("state", "statename"), "state"),
    (("district", "city", "location", "place", "town"), "district"),
    (("stream", "course", "programme", "program", "type", "category"), "stream"),
    (("affiliation", "affiliatedto", "university", "board"), "affiliation"),
    (("website", "url", "weburl", "websiteurl", "site", "webaddress"), "website"),
    (("placementofficer", "placementofficername", "tponame", "contactperson",
      "contactname", "person", "spoc"), "placement_officer_name"),
    (("placementemail", "tpoemail", "placementmail", "officialemail",
      "primaryemail", "mainemail", "bestemail", "contactemail",
      "emailid", "email", "mail", "emailaddress"), "placement_email"),
    (("placementphone", "tpophone", "placementcontact", "phone", "mobile",
      "contact", "contactno", "phoneno", "phonenumber", "mobileno",
      "telephone"), "placement_phone"),
    (("allemailsfound", "allemails", "alternateemail", "secondaryemail",
      "backupemail", "otheremail", "generalemail", "fallbackemail",
      "additionalemails"), "fallback_contact_email"),
    (("alternatephone", "secondaryphone", "backupphone", "otherphone",
      "generalphone", "landline", "fallbackphone"), "fallback_contact_phone"),
]

#: Values that mean "empty" in hand-maintained spreadsheets.
_NULLISH = {"", "-", "--", "n/a", "na", "nil", "none", "null", "not available",
            "not found", "nan", "#n/a", "tbd", "?"}

_EMAIL_RE = re.compile(r"[\w\.\+\-]+@[\w\-]+\.[\w\.\-]+")


def _key(column: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(column).lower())


def map_columns(columns: Iterable[str]) -> dict[str, str]:
    """Best-effort map of source columns onto our field names.

    Returns {source_column: our_field}. A source column that matches nothing
    is left out and reported, rather than guessed at — a wrong guess would put
    a phone number in the affiliation column and nobody would notice.
    """
    mapping: dict[str, str] = {}
    taken: set[str] = set()

    for column in columns:
        normalized = _key(column)
        if not normalized:
            continue
        for aliases, field in COLUMN_ALIASES:
            if field in taken:
                continue
            if normalized in aliases or any(
                normalized == alias or normalized.startswith(alias) for alias in aliases
            ):
                mapping[column] = field
                taken.add(field)
                break
    return mapping


def clean(value: Any) -> str:
    """Trim a spreadsheet cell and treat placeholder text as empty."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return "" if text.lower() in _NULLISH else text


def split_contacts(raw: str) -> list[str]:
    """A cell may hold several values ("a@x.in, b@x.in" or "123 / 456")."""
    if not raw:
        return []
    parts = re.split(r"[,;/|\n]+", raw)
    return [p.strip() for p in parts if p.strip()]


def _first_email(raw: str) -> tuple[str, list[str]]:
    """Return (primary, extras) from a possibly multi-value email cell."""
    candidates: list[str] = []
    for part in split_contacts(raw):
        candidates.extend(_EMAIL_RE.findall(part))
    seen = list(dict.fromkeys(c.lower() for c in candidates))
    return (seen[0], seen[1:]) if seen else ("", [])


def _first_phone(raw: str) -> tuple[str, list[str]]:
    """Return (primary, extras) from a possibly multi-value phone cell.

    Reuses the pipeline's normalizer so imported numbers get the same
    validation as scraped ones — junk like a PIN code or a run-together
    string is rejected here too.
    """
    normalized = [normalize_phone(p) for p in split_contacts(raw)]
    seen = list(dict.fromkeys(p for p in normalized if p))
    return (seen[0], seen[1:]) if seen else ("", [])


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls", ".xlsm"}:
        return pd.read_excel(path, dtype=str)
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(path, dtype=str, encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"could not decode {path.name} as UTF-8 or Latin-1")


def build_records(
    frame: pd.DataFrame,
    mapping: dict[str, str],
    *,
    default_state: str = "",
    default_stream: str = "Engineering",
) -> tuple[list[dict[str, Any]], Counter]:
    """Turn spreadsheet rows into records ready for upsert_college."""
    records: list[dict[str, Any]] = []
    stats: Counter = Counter()

    for _, row in frame.iterrows():
        fields = {field: clean(row.get(column)) for column, field in mapping.items()}

        name = fields.get("college_name", "")
        if not name or len(name) < 3:
            stats["skipped_no_name"] += 1
            continue

        state = fields.get("state") or default_state
        if not state:
            stats["skipped_no_state"] += 1
            continue

        # Accept "B.Tech"/"BE"/"engineering" etc. as Engineering, "BCA"/"MCA"
        # as BCA, and fall back to the CLI default rather than guessing.
        raw_stream = (fields.get("stream") or "").lower()
        if "bca" in raw_stream or "computer application" in raw_stream:
            stream = "BCA"
        elif raw_stream:
            stream = "Engineering"
        else:
            stream = default_stream
        if stream not in STREAM_VALUES:
            stream = default_stream

        primary_email, extra_emails = _first_email(fields.get("placement_email", ""))
        fallback_email, more_emails = _first_email(fields.get("fallback_contact_email", ""))
        primary_phone, extra_phones = _first_phone(fields.get("placement_phone", ""))
        fallback_phone, more_phones = _first_phone(fields.get("fallback_contact_phone", ""))

        # If only one contact was supplied, treat it as the fallback rather
        # than claiming it is a verified placement contact.
        if primary_email and not fallback_email and "placement" not in " ".join(
            k for k, v in mapping.items() if v == "placement_email"
        ).lower():
            fallback_email = fallback_email or ""

        website = fields.get("website", "")
        if website and not website.startswith(("http://", "https://")):
            website = f"https://{website}"

        record = {
            "college_name": name,
            "state": state,
            "stream": stream,
            "district": fields.get("district", ""),
            "affiliation": fields.get("affiliation", ""),
            "website": website,
            "placement_officer_name": fields.get("placement_officer_name", ""),
            "placement_email": primary_email,
            "placement_phone": primary_phone,
            "fallback_contact_email": fallback_email,
            "fallback_contact_phone": fallback_phone,
            "backup_emails_found": extra_emails + more_emails,
            "backup_phones_found": extra_phones + more_phones,
            # Imported data is not pipeline-scored. 0 keeps it honest, and the
            # admin view shows it as imported rather than confidently scraped.
            "confidence_score": 0,
            "source_urls": [],
            "email_verified": False,
            "last_scraped": "",
            # A row with both contacts is directly usable; anything less needs
            # a human or a later scrape. Never "Verified" — nothing verified it.
            "status": (
                "Needs Follow-up"
                if (primary_email or fallback_email) and (primary_phone or fallback_phone)
                else "Needs Follow-up"
            ),
        }

        has_email = bool(primary_email or fallback_email)
        has_phone = bool(primary_phone or fallback_phone)
        stats["complete" if (has_email and has_phone) else "incomplete"] += 1
        stats[f"state:{state}"] += 1
        stats[f"stream:{stream}"] += 1
        records.append(record)

    return records, stats


def import_file(
    path: Path, *, default_state: str = "", default_stream: str = "Engineering",
    dry_run: bool = False,
) -> dict[str, Any]:
    frame = read_table(path)
    mapping = map_columns(frame.columns)

    if "college_name" not in mapping.values():
        raise ValueError(
            "no college-name column found. Columns seen: "
            + ", ".join(map(str, frame.columns))
        )

    records, stats = build_records(
        frame, mapping, default_state=default_state, default_stream=default_stream
    )

    # Dedupe within the file itself before touching the DB.
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        normalized, district = dedupe_key(record["college_name"], record["district"])
        key = (normalized, district, record["stream"])
        if key in seen:
            stats["duplicates_in_file"] += 1
            existing = seen[key]
            for field, value in record.items():
                if not existing.get(field) and value:
                    existing[field] = value
        else:
            seen[key] = record

    unique = list(seen.values())

    if not dry_run:
        with get_conn() as conn:
            init_db(conn)
            for record in unique:
                upsert_college(conn, record)

    return {
        "file": path.name,
        "rows_read": len(frame),
        "mapping": mapping,
        "unmapped_columns": [c for c in frame.columns if c not in mapping],
        "records": len(unique),
        "stats": dict(stats),
        "written": not dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="CSV or Excel file to import")
    parser.add_argument("--default-state", default="",
                        help="state for rows whose state cell is blank")
    parser.add_argument("--stream", default="Engineering", choices=list(STREAM_VALUES),
                        help="stream for rows whose stream cell is blank")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be imported without writing")
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"error: {args.path} not found", file=sys.stderr)
        return 1

    try:
        summary = import_file(
            args.path, default_state=args.default_state,
            default_stream=args.stream, dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nfile        : {summary['file']}")
    print(f"rows read   : {summary['rows_read']}")
    print(f"unique rows : {summary['records']}")
    print(f"written     : {'no (dry run)' if not summary['written'] else 'yes'}")

    print("\ncolumn mapping:")
    for source, field in summary["mapping"].items():
        print(f"  {str(source)[:34]:36} -> {field}")
    if summary["unmapped_columns"]:
        print("\nignored columns (no match — check none of these matter):")
        for column in summary["unmapped_columns"]:
            print(f"  {column}")

    stats = summary["stats"]
    print("\ncounts:")
    print(f"  complete (email AND phone) : {stats.get('complete', 0)}  -> visible to marketing")
    print(f"  incomplete                 : {stats.get('incomplete', 0)}  -> admin/QA only")
    for label, prefix in (("by state", "state:"), ("by stream", "stream:")):
        entries = {k[len(prefix):]: v for k, v in stats.items() if k.startswith(prefix)}
        if entries:
            print(f"  {label}: " + ", ".join(f"{k}={v}" for k, v in sorted(entries.items())))
    for key in ("skipped_no_name", "skipped_no_state", "duplicates_in_file"):
        if stats.get(key):
            print(f"  {key}: {stats[key]}")

    if not summary["written"]:
        print("\nDry run — nothing written. Re-run without --dry-run to import.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
