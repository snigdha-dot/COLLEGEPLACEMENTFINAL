"""Tests for the dataset importer.

The importer writes through the same repository the scraper uses, so the rules
under test are that it cannot smuggle a row past the completeness filter, and
cannot corrupt or overwrite scraped data.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.import_data import (  # noqa: E402
    _first_email,
    _first_phone,
    build_records,
    clean,
    map_columns,
    split_contacts,
)


def test_column_mapping_handles_real_world_headers() -> None:
    mapping = map_columns([
        "Name of the College", "State", "City", "Affiliated To", "Web URL",
        "Contact Person", "Email ID", "Mobile No", "Course", "Remarks",
    ])
    assert mapping["Name of the College"] == "college_name"
    assert mapping["City"] == "district"
    assert mapping["Email ID"] == "placement_email"
    assert mapping["Mobile No"] == "placement_phone"
    assert mapping["Course"] == "stream"
    # An unrecognised column is left unmapped rather than guessed at: a wrong
    # guess would silently put the wrong data in a contact field.
    assert "Remarks" not in mapping


def test_column_mapping_is_case_and_punctuation_insensitive() -> None:
    for header in ["college_name", "COLLEGE NAME", "College-Name", "collegename"]:
        assert map_columns([header])[header] == "college_name"


def test_placeholder_text_treated_as_empty() -> None:
    for junk in ["N/A", "n/a", "-", "--", "NIL", "none", "Not Available", "TBD", "?"]:
        assert clean(junk) == "", junk
    assert clean("  Real   Value  ") == "Real Value"


def test_multi_value_cells_split() -> None:
    assert split_contacts("a@x.in, b@x.in") == ["a@x.in", "b@x.in"]
    assert split_contacts("98765 / 12345") == ["98765", "12345"]
    assert split_contacts("") == []


def test_first_email_returns_primary_and_extras() -> None:
    primary, extras = _first_email("placement@rvce.edu.in, tpo@rvce.edu.in")
    assert primary == "placement@rvce.edu.in"
    assert extras == ["tpo@rvce.edu.in"]
    assert _first_email("not an email") == ("", [])


def test_first_phone_uses_pipeline_validation() -> None:
    """Imported numbers get the same validation as scraped ones."""
    primary, extras = _first_phone("+91 98765 43210 / 080-26622130")
    assert primary == "+91-9876543210"
    assert extras == ["+91-8026622130"]
    # A year, a PIN code, and run-together digits are not phone numbers.
    assert _first_phone("2026") == ("", [])
    assert _first_phone("560001") == ("", [])
    assert _first_phone("802662213035") == ("", [])


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_rows_without_a_name_are_skipped() -> None:
    frame = _frame([
        {"College": "", "State": "Karnataka", "Email": "x@y.com", "Phone": "9876543210"},
        {"College": "Real College", "State": "Karnataka", "Email": "x@y.com",
         "Phone": "9876543210"},
    ])
    records, stats = build_records(frame, map_columns(frame.columns))
    assert len(records) == 1
    assert stats["skipped_no_name"] == 1


def test_stream_detected_from_course_column() -> None:
    frame = _frame([
        {"College": "A College", "State": "Karnataka", "Course": "B.Tech"},
        {"College": "B College", "State": "Karnataka", "Course": "BCA"},
        {"College": "C College", "State": "Karnataka", "Course": ""},
    ])
    records, _ = build_records(frame, map_columns(frame.columns),
                               default_stream="Engineering")
    assert [r["stream"] for r in records] == ["Engineering", "BCA", "Engineering"]


def test_jumbled_states_are_preserved_per_row() -> None:
    """The whole point: one mixed file, split correctly by state."""
    frame = _frame([
        {"College": "A College", "State": "Karnataka"},
        {"College": "B College", "State": "Tamil Nadu"},
        {"College": "C College", "State": "Andhra Pradesh"},
    ])
    records, _ = build_records(frame, map_columns(frame.columns))
    assert {r["state"] for r in records} == {"Karnataka", "Tamil Nadu", "Andhra Pradesh"}


def test_default_state_fills_blank_cells_only() -> None:
    frame = _frame([
        {"College": "A College", "State": ""},
        {"College": "B College", "State": "Tamil Nadu"},
    ])
    records, _ = build_records(frame, map_columns(frame.columns),
                               default_state="Karnataka")
    assert records[0]["state"] == "Karnataka"
    assert records[1]["state"] == "Tamil Nadu", "explicit state must not be overridden"


def test_imported_rows_are_never_marked_verified() -> None:
    """Nothing verified an imported contact, so it must not claim otherwise."""
    frame = _frame([{"College": "A College", "State": "Karnataka",
                     "Email": "tpo@a.ac.in", "Phone": "9876543210"}])
    records, _ = build_records(frame, map_columns(frame.columns))
    assert records[0]["status"] != "Verified"
    assert records[0]["confidence_score"] == 0
    assert records[0]["email_verified"] is False


def test_import_never_presets_outreach_status() -> None:
    """outreach_status belongs to marketing; the importer must not set it."""
    frame = _frame([{"College": "A College", "State": "Karnataka",
                     "Email": "tpo@a.ac.in", "Phone": "9876543210"}])
    records, _ = build_records(frame, map_columns(frame.columns))
    assert "outreach_status" not in records[0]


def test_website_gets_a_scheme() -> None:
    frame = _frame([{"College": "A College", "State": "Karnataka",
                     "Website": "rvce.edu.in"}])
    records, _ = build_records(frame, map_columns(frame.columns))
    assert records[0]["website"] == "https://rvce.edu.in"


def test_end_to_end_import_applies_completeness_filter() -> None:
    """A row missing a phone must land in the DB but not reach marketing."""
    import os

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_PATH"] = str(Path(tmp) / "t.db")
        # Imported lazily so DATABASE_PATH is read after it is set.
        from backend.db import repository as repo
        from backend.db.session import get_conn
        from backend.import_data import import_file

        source = Path(tmp) / "data.xlsx"
        pd.DataFrame([
            {"College": "Complete College", "State": "Karnataka",
             "Email": "tpo@complete.ac.in", "Phone": "9876543210"},
            {"College": "No Phone College", "State": "Tamil Nadu",
             "Email": "tpo@nophone.ac.in", "Phone": "N/A"},
        ]).to_excel(source, index=False)

        summary = import_file(source)
        assert summary["records"] == 2

        with get_conn() as conn:
            assert len(repo.admin_rows(conn)) == 2, "both rows must be stored"
            marketing = repo.marketing_rows(conn)
            assert len(marketing) == 1, "incomplete row leaked to marketing"
            assert marketing[0]["college_name"] == "Complete College"


def test_duplicate_rows_merge_within_the_file() -> None:
    import os

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_PATH"] = str(Path(tmp) / "t2.db")
        from backend.db import repository as repo
        from backend.db.session import get_conn
        from backend.import_data import import_file

        source = Path(tmp) / "dupes.xlsx"
        pd.DataFrame([
            {"College": "R.V. College of Engineering", "State": "Karnataka",
             "City": "Bengaluru", "Email": "placement@rvce.edu.in", "Phone": ""},
            {"College": "RV College of Engineering, Bengaluru", "State": "Karnataka",
             "City": "Bengaluru", "Email": "", "Phone": "9845012345"},
        ]).to_excel(source, index=False)

        import_file(source)
        with get_conn() as conn:
            rows = repo.admin_rows(conn)
            assert len(rows) == 1, "name variants should merge into one college"
            # The merge fills blanks from the second row rather than dropping it.
            row = rows[0]
            assert row["placement_email"] == "placement@rvce.edu.in"
            assert row["placement_phone"] == "+91-9845012345"


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
