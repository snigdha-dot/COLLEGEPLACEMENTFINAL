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


def _agg(name: str, district: str = "Bengaluru Urban", **kw) -> SeedCollege:
    return SeedCollege(college_name=name, state="Karnataka", stream="Engineering",
                       district=district, source="aggregator", **kw)


def test_clean_listing_name_strips_decoration() -> None:
    cases = [
        ("RV College of Engineering - Admission 2026, Fees, Cutoff",
         "RV College of Engineering"),
        ("12. BMS College of Engineering", "BMS College of Engineering"),
        ("Dayananda Sagar College of Engineering (Bangalore)",
         "Dayananda Sagar College of Engineering"),
        ("  Nitte   Meenakshi Institute of Technology  ",
         "Nitte Meenakshi Institute of Technology"),
    ]
    for raw, expected in cases:
        assert sb.clean_listing_name(raw) == expected, f"{raw!r} -> {sb.clean_listing_name(raw)!r}"


def test_clean_listing_name_rejects_non_institutions() -> None:
    """Navigation links and article titles must not become seed rows."""
    for junk in ["Home", "Login / Register", "Read More", "Top 10 Courses After 12th",
                 "Privacy Policy", "About Us", "2026", "", "Compare Colleges"]:
        assert sb.clean_listing_name(junk) == "", f"should reject {junk!r}"


def test_college_of_pattern_survives_category_filter() -> None:
    """REGRESSION: the category rule `colleges?\\s+(in|of|...)` matched
    "College of Engineering" — the most common Indian college naming pattern —
    and silently rejected real colleges. Recovering these raised the live
    Karnataka yield from 52 to 66."""
    for name in [
        "BMS College of Engineering",
        "RV College of Engineering",
        "Dayananda Sagar College of Engineering",
        "University of Visvesvaraya College of Engineering",
        "Sri Jayachamarajendra College of Engineering",
    ]:
        assert sb.clean_listing_name(name) == name, f"real college rejected: {name!r}"


def test_plural_category_headings_still_rejected() -> None:
    """The singular/plural distinction is what separates these from the above."""
    for junk in [
        "Engineering Colleges in Pune",
        "Engineering Colleges in India Accepting GATE",
        "Top Colleges in India",
        "Universities in Karnataka",
        "KCET College Predictor",
        "Civil Engineering",
    ]:
        assert sb.clean_listing_name(junk) == "", f"category leaked through: {junk!r}"


def test_acronym_only_colleges_are_kept() -> None:
    """An initialism is a valid distinguishing token — "RV" and "BMS" are the
    only non-generic word in those names, and dropping them loses real leads."""
    for name in ["RV College of Engineering", "BMS College of Engineering",
                 "PES University, Bangalore"]:
        assert sb.clean_listing_name(name), f"acronym-named college dropped: {name!r}"


def test_alias_prefix_stripped_only_when_it_is_an_alias() -> None:
    assert sb.clean_listing_name(
        "MSRIT Bangalore - Ramaiah Institute of Technology, Bangalore"
    ) == "Ramaiah Institute of Technology, Bangalore"
    assert sb.clean_listing_name(
        "SIT Tumkur - Siddaganga Institute of Technology, Tumkur"
    ) == "Siddaganga Institute of Technology, Tumkur"
    # Not an alias pattern: the first half is itself the college name.
    assert sb.clean_listing_name("RV College of Engineering - Fees") == \
        "RV College of Engineering"


def test_plausibly_in_state_rejects_foreign_and_other_states() -> None:
    for name in ["Massachusetts Institute of Technology", "Stanford University",
                 "Brunel University", "Edge Hill University"]:
        assert not sb.plausibly_in_state(name, "Karnataka"), f"foreign kept: {name!r}"


def test_plausibly_in_state_keeps_local_and_placeless_names() -> None:
    """Most real college names carry no place at all, so a missing place must
    not be treated as disqualifying."""
    for name in ["Siddaganga Institute of Technology",
                 "RV College of Engineering, Bengaluru",
                 "Manipal Institute of Technology, Manipal",
                 "National Institute of Technology Karnataka Surathkal"]:
        assert sb.plausibly_in_state(name, "Karnataka"), f"local rejected: {name!r}"


def test_aggregator_host_detection() -> None:
    assert sb.is_aggregator("https://collegedunia.com/engineering/karnataka")
    assert sb.is_aggregator("https://www.shiksha.com/b-tech/colleges/karnataka")
    assert not sb.is_aggregator("https://vtu.ac.in/affiliated-institute")


def test_aggregator_data_never_overwrites_trusted_contacts() -> None:
    """The trust boundary: aggregator rows contribute names, never contacts."""
    maps = [_maps("RV College of Engineering", phone="+91-80-REAL", address="Real Address")]
    # Even if an aggregator row somehow carried contact data, merging must not
    # let it touch the Maps-sourced fields.
    aggregator = [_agg("R.V. College of Engineering", phone="+91-WRONG",
                       address="Wrong Address", website="https://wrong.example")]

    merged = merge_and_dedupe(maps, [], aggregator)
    assert len(merged) == 1
    row = merged[0]
    assert row.phone == "+91-80-REAL", "aggregator overwrote a trusted phone"
    assert row.address == "Real Address", "aggregator overwrote a trusted address"
    assert row.website != "https://wrong.example"
    assert "aggregator" in row.source


def test_aggregator_only_rows_have_no_contact_data() -> None:
    """A college known only from an aggregator starts with empty contacts."""
    merged = merge_and_dedupe([], [], [_agg("Some College of Engineering")])
    assert len(merged) == 1
    assert merged[0].phone == ""
    assert merged[0].website == ""
    assert merged[0].address == ""


def test_candidate_names_extracted_from_headings_and_links() -> None:
    html = """
      <h2>RV College of Engineering - Fees</h2>
      <a href="/x">BMS College of Engineering</a>
      <a href="/login">Login</a>
      <h3>Top Courses After 12th</h3>
    """
    names = [sb.clean_listing_name(n) for n in sb._candidate_names_from_html(html)]
    kept = [n for n in names if n]
    assert "RV College of Engineering" in kept
    assert "BMS College of Engineering" in kept
    assert all("Login" not in n for n in kept)


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
