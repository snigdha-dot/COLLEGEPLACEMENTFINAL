"""Tests for the master list builder's merge, cache, and filtering logic.

Fully mocked — no credits spent. Covers the parts most likely to silently
corrupt a seed list: the two-channel merge (Maps must win for phone/address),
the authoritative-source filter, and cache TTL behavior.
"""

from __future__ import annotations

import csv
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.scraper.seed_builder as sb  # noqa: E402
from backend.scraper.seed_builder import (  # noqa: E402
    SeedCollege,
    _colleges_from_tables,
    directory_queries,
    is_authoritative,
    merge_and_dedupe,
    read_cache,
    write_cache,
)


def _maps(name: str, district: str = "Bengaluru Urban", **kw) -> SeedCollege:
    return SeedCollege(college_name=name, state="Karnataka", stream="Engineering",
                       district=district, source="maps", **kw)


def _dir(name: str, district: str = "Bengaluru Urban", **kw) -> SeedCollege:
    return SeedCollege(college_name=name, state="Karnataka", stream="Engineering",
                       district=district, source="directory", **kw)


def test_authoritative_filter_rejects_aggregators() -> None:
    for url in [
        "https://collegedunia.com/engineering/karnataka/aicte-approved-colleges",
        "https://zollege.in/engineering/karnataka",
        "https://targetstudy.com/colleges/",
        "https://www.bing.com/aclk?ld=e8L8",
        "https://en.wikipedia.org/wiki/List_of_colleges",
    ]:
        assert not is_authoritative(url), f"should reject {url}"


def test_authoritative_filter_accepts_official_sources() -> None:
    for url in [
        "https://dtekarnataka.gov.in/colleges",
        "https://vtu.ac.in/en/affiliated-colleges/",
        "https://vtu.ac.in/affiliated-institute",
        "https://vtu.ac.in/autonomous-colleges",
        "https://www.aicte-india.org/approved-institutes",
        "https://kea.kar.nic.in/college_list",
    ]:
        assert is_authoritative(url), f"should accept {url}"


def test_authoritative_filter_rejects_unrelated_government_pages() -> None:
    """A .gov.in domain alone is not enough — both of these scored as
    authoritative before the listing-hint requirement was added (2026-08-02)."""
    for url in [
        "https://karnataka.gov.in/index.php/english",
        "https://www.incredibleindia.gov.in/en/karnataka",
        "https://www.india.gov.in/",
    ]:
        assert not is_authoritative(url), f"should reject unrelated gov page {url}"


def test_directory_queries_avoid_site_operator() -> None:
    """/v1/search returned 502 on every query containing `site:` (2026-08-02)."""
    for query in directory_queries("Karnataka", "Engineering"):
        assert "site:" not in query, f"site: operator breaks /v1/search: {query!r}"


def test_merge_prefers_maps_for_phone_and_address() -> None:
    """The brief: prefer the Maps entry for phone/address when both agree."""
    maps = [_maps("R.V. College of Engineering", phone="+91-80-1111", address="Mysore Rd")]
    directory = [_dir("RV College of Engineering", affiliation="VTU",
                      source_urls=["https://vtu.ac.in/x"])]

    merged = merge_and_dedupe(maps, directory)
    assert len(merged) == 1, "name variants should merge into one row"
    row = merged[0]
    assert row.phone == "+91-80-1111"
    assert row.address == "Mysore Rd"
    assert row.affiliation == "VTU", "directory should still contribute affiliation"
    assert "directory" in row.source and "maps" in row.source


def test_merge_keeps_distinct_colleges_apart() -> None:
    merged = merge_and_dedupe(
        [_maps("BMS College of Engineering")],
        [_dir("BMS Institute of Technology")],
    )
    assert len(merged) == 2, "different colleges must not merge"


def test_merge_separates_same_name_in_different_districts() -> None:
    merged = merge_and_dedupe(
        [_maps("Government Engineering College", district="Hassan")],
        [_dir("Government Engineering College", district="Ramanagara")],
    )
    assert len(merged) == 2


def test_merge_with_empty_maps_channel() -> None:
    """The current reality: Maps disabled, directory alone."""
    merged = merge_and_dedupe([], [_dir("A College of Engineering"), _dir("B Institute")])
    assert len(merged) == 2
    assert all(c.source == "directory" for c in merged)


def test_colleges_from_tables_identifies_name_column() -> None:
    tables = {"tables": [{
        "headers": ["Sl. No.", "Name of the College", "District", "Intake"],
        "rows": [
            ["1", "R.V. College of Engineering", "Bengaluru Urban", "1200"],
            ["2", "BMS College of Engineering", "Bengaluru Urban", "900"],
        ],
    }]}
    out = _colleges_from_tables(tables, "Karnataka", "Engineering", "https://vtu.ac.in/x")
    assert [c.college_name for c in out] == [
        "R.V. College of Engineering", "BMS College of Engineering"]
    assert out[0].district == "Bengaluru Urban"
    assert out[0].source_urls == ["https://vtu.ac.in/x"]


def test_colleges_from_tables_skips_unidentifiable_tables() -> None:
    """A table with no name-like header is skipped, not guessed at."""
    tables = {"tables": [{"headers": ["Year", "Intake", "Fees"],
                          "rows": [["2024", "120", "50000"]]}]}
    assert _colleges_from_tables(tables, "Karnataka", "Engineering", "u") == []


def test_colleges_from_tables_skips_short_junk_rows() -> None:
    tables = {"tables": [{
        "headers": ["College Name"],
        "rows": [["Total"], ["-"], ["12"], ["Valid Engineering College Name"]],
    }]}
    out = _colleges_from_tables(tables, "Karnataka", "Engineering", "u")
    assert [c.college_name for c in out] == ["Valid Engineering College Name"]


def test_cache_round_trip_and_ttl(tmp_dir: Path | None = None) -> None:
    original = sb.SEED_LIST_DIR
    with tempfile.TemporaryDirectory() as tmp:
        sb.SEED_LIST_DIR = Path(tmp)
        try:
            colleges = [_maps("Test College of Engineering", phone="+91-1",
                              website="https://t.ac.in")]
            path = write_cache("Karnataka", "Engineering", colleges)
            assert path.exists()

            loaded = read_cache("Karnataka", "Engineering")
            assert loaded is not None and len(loaded) == 1
            assert loaded[0].college_name == "Test College of Engineering"
            assert loaded[0].phone == "+91-1"

            # A stale timestamp must invalidate the cache.
            rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
            stale = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
            for row in rows:
                row["generated_at"] = stale
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            assert read_cache("Karnataka", "Engineering", ttl_days=30) is None
            assert read_cache("Karnataka", "Engineering", ttl_days=60) is not None
        finally:
            sb.SEED_LIST_DIR = original


def test_cache_miss_returns_none() -> None:
    original = sb.SEED_LIST_DIR
    with tempfile.TemporaryDirectory() as tmp:
        sb.SEED_LIST_DIR = Path(tmp)
        try:
            assert read_cache("Nowhere", "Engineering") is None
        finally:
            sb.SEED_LIST_DIR = original


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
