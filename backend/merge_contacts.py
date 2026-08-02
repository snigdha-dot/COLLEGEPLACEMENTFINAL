"""Merge a phone-contacts file into an existing college dataset.

Two spreadsheets arrive separately: one with colleges + emails, one with
colleges + phones. This joins them on the same dedupe key the rest of the
system uses (normalized name + state + stream) so the result is a single file
ready for import_data.py.

Matching is deliberately staged, strictest first, and every stage is reported:

  1. exact   — normalized name + state + stream all agree
  2. name+state — same college, stream differs (a college often appears under
                  both Engineering and BCA; a phone number is the same either way)
  3. name-only  — same normalized name, state differs. Reported but NOT applied
                  by default: "Nalanda Degree College Vijayawada" appears under
                  two states in these files, and guessing would attach a phone
                  to the wrong institution.

Anything unmatched is listed rather than silently dropped, so a low match rate
is visible instead of looking like success.

Usage:
    python -m backend.merge_contacts colleges.csv contacts.csv -o merged.csv
    python -m backend.merge_contacts colleges.csv contacts.csv --report-only
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from .import_data import clean, map_columns, read_table, split_contacts
from .scraper.contact_extractor import normalize_phone
from .scraper.normalize import normalize_name

#: Column aliases for the phones file. Reuses import_data's mapping for the
#: identifying columns and adds the phone-specific ones.
_PHONE_ALIASES = (
    "allphonesfound", "allphones", "phones", "phone", "mobile", "contact",
    "contactno", "phoneno", "phonenumber", "mobileno", "telephone", "landline",
)


def _phone_column(columns: list[str]) -> str | None:
    import re

    for column in columns:
        key = re.sub(r"[^a-z0-9]", "", str(column).lower())
        if key in _PHONE_ALIASES:
            return column
    return None


#: Both source files contain names where a literal "nan" was stripped by a
#: spreadsheet tool treating it as a null value: "Daya[]da Sagar" (Dayananda),
#: "[]dha Engineering" (Nandha), "Kirupa[]da" (Kirupananda), "Siva[]da"
#: (Sivananda), "J[]avikasa" (Jnanavikasa). Restoring it lets a corrupted name
#: in one file match the same corrupted — or intact — name in the other.
def _repair_placeholder(name: str) -> str:
    return name.replace("[]", "nan") if "[]" in name else name


def _normalize(name: str) -> str:
    return normalize_name(_repair_placeholder(name))


def _phones_from_cell(raw: str) -> list[str]:
    """Pull every dialable number out of one messy cell.

    Separators in these files are inconsistent: commas usually, but sometimes
    only a space ("09999 9611277233" is two numbers, not one 16-digit string).
    So each comma-separated part is tried whole first — a number may legitimately
    contain spaces, as in "+91 884 230 0900" — and only if that fails is the
    part split on whitespace and each piece tried separately.
    """
    found: list[str] = []
    for part in split_contacts(raw):
        whole = normalize_phone(part)
        if whole:
            found.append(whole)
            continue
        for piece in part.split():
            candidate = normalize_phone(piece)
            if candidate:
                found.append(candidate)
    # Order preserved; the first number becomes the primary contact.
    return list(dict.fromkeys(found))


def _key(name: str, state: str, stream: str) -> tuple[str, str, str]:
    return (_normalize(name), state.strip().lower(), stream.strip().lower())


def load_phone_index(path: Path) -> tuple[dict, dict, dict, int]:
    """Build lookup indexes from the contacts file.

    Returns (by_name_state_stream, by_name_state, by_name, rows_with_phones).
    """
    frame = read_table(path)
    mapping = map_columns(frame.columns)
    inverted = {field: column for column, field in mapping.items()}

    name_col = inverted.get("college_name")
    state_col = inverted.get("state")
    stream_col = inverted.get("stream")
    phone_col = _phone_column(list(frame.columns))

    if not name_col:
        raise ValueError(f"no college-name column in {path.name}")
    if not phone_col:
        raise ValueError(
            f"no phone column in {path.name}. Columns: {', '.join(map(str, frame.columns))}"
        )

    exact: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    name_state: dict[tuple[str, str], list[str]] = defaultdict(list)
    name_only: dict[str, list[str]] = defaultdict(list)
    with_phones = 0

    for _, row in frame.iterrows():
        name = clean(row.get(name_col))
        if not name:
            continue
        state = clean(row.get(state_col)) if state_col else ""
        stream = clean(row.get(stream_col)) if stream_col else ""

        phones = _phones_from_cell(clean(row.get(phone_col)))
        if not phones:
            continue
        with_phones += 1

        normalized = _normalize(name)
        exact[_key(name, state, stream)].extend(phones)
        name_state[(normalized, state.strip().lower())].extend(phones)
        name_only[normalized].extend(phones)

    return dict(exact), dict(name_state), dict(name_only), with_phones


def merge(
    colleges_path: Path, contacts_path: Path, *, allow_name_only: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Attach phones to the colleges file. Returns (frame, report)."""
    exact, name_state, name_only, contact_rows = load_phone_index(contacts_path)

    frame = read_table(colleges_path)
    mapping = map_columns(frame.columns)
    inverted = {field: column for column, field in mapping.items()}

    name_col = inverted.get("college_name")
    if not name_col:
        raise ValueError(f"no college-name column in {colleges_path.name}")
    state_col = inverted.get("state")
    stream_col = inverted.get("stream")

    matched_exact = matched_name_state = matched_name_only = 0
    unmatched: list[str] = []
    phone_values: list[str] = []

    for _, row in frame.iterrows():
        name = clean(row.get(name_col))
        state = clean(row.get(state_col)) if state_col else ""
        stream = clean(row.get(stream_col)) if stream_col else ""
        normalized = _normalize(name)

        phones: list[str] = []
        if (hit := exact.get(_key(name, state, stream))):
            phones, matched_exact = hit, matched_exact + 1
        elif (hit := name_state.get((normalized, state.strip().lower()))):
            phones, matched_name_state = hit, matched_name_state + 1
        elif allow_name_only and (hit := name_only.get(normalized)):
            phones, matched_name_only = hit, matched_name_only + 1
        else:
            if name:
                unmatched.append(name)

        # Deduplicate, order preserved: the first number is the primary contact.
        phone_values.append(", ".join(dict.fromkeys(phones)))

    frame["all_phones_found"] = phone_values

    report = {
        "college_rows": len(frame),
        "contact_rows_with_phones": contact_rows,
        "matched_exact": matched_exact,
        "matched_name_state": matched_name_state,
        "matched_name_only": matched_name_only,
        "matched_total": matched_exact + matched_name_state + matched_name_only,
        "unmatched": len(unmatched),
        "unmatched_sample": unmatched[:25],
    }
    return frame, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("colleges", type=Path, help="file with colleges + emails")
    parser.add_argument("contacts", type=Path, help="file with colleges + phones")
    parser.add_argument("-o", "--out", type=Path, help="write the merged CSV here")
    parser.add_argument(
        "--allow-name-only", action="store_true",
        help="also match on name alone when the state differs (riskier)",
    )
    parser.add_argument("--report-only", action="store_true",
                        help="print the match report without writing")
    args = parser.parse_args(argv)

    for path in (args.colleges, args.contacts):
        if not path.exists():
            print(f"error: {path} not found", file=sys.stderr)
            return 1

    try:
        frame, report = merge(
            args.colleges, args.contacts, allow_name_only=args.allow_name_only
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\ncolleges file : {report['college_rows']} rows")
    print(f"contacts file : {report['contact_rows_with_phones']} rows with usable phones")
    print()
    print("matches:")
    print(f"  name + state + stream : {report['matched_exact']}")
    print(f"  name + state          : {report['matched_name_state']}")
    if args.allow_name_only:
        print(f"  name only             : {report['matched_name_only']}")
    print(f"  TOTAL MATCHED         : {report['matched_total']}")
    print(f"  unmatched             : {report['unmatched']}")

    if report["unmatched_sample"]:
        print("\nunmatched colleges (first 25 — these will have no phone):")
        for name in report["unmatched_sample"]:
            print(f"  {name[:70]}")

    if args.out and not args.report_only:
        frame.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\nwrote {args.out}")
    elif not args.report_only:
        print("\nNo -o given; nothing written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
