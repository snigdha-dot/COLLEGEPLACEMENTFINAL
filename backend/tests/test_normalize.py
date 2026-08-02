"""Tests for seed-list dedupe normalization.

Two failure modes, and they are not symmetric:
  - SPLIT: a duplicate survives as two rows. Annoying; a human spots it.
  - COLLIDE: two different colleges merge into one row. A real lead is lost
    silently and nobody ever knows.

Collisions are the ones that matter, so they get the larger test set. All
fixtures are real Karnataka colleges, since Karnataka is the pilot state.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.scraper.normalize import dedupe_key, normalize_name  # noqa: E402

#: Spelling variants of the SAME college — must produce one key each group.
SAME_COLLEGE = [
    ["R.V. College of Engineering", "RV College of Engineering",
     "RV College of Engineering, Bengaluru", "R V College of Engineering"],
    ["B.M.S. College of Engineering", "BMS College of Engineering, Bangalore"],
    ["Sri Siddhartha Institute of Technology", "Siddhartha Inst. of Tech.",
     "SRI SIDDHARTHA INSTITUTE OF TECHNOLOGY"],
    ["M S Ramaiah Institute of Technology",
     "M.S. Ramaiah Institute of Technology, Bengaluru"],
    ["Dayananda Sagar College of Engineering", "Dayananda Sagar College of Engg"],
    ["P.E.S. Institute of Technology", "PES Institute of Technology"],
    ["Nitte Meenakshi Institute of Technology  ", "nitte meenakshi institute of technology"],
]

#: DIFFERENT colleges that share a prefix — must never collapse together.
DIFFERENT_COLLEGES = [
    ("BMS College of Engineering", "BMS Institute of Technology"),
    ("Acharya Institute of Technology", "Acharya Institute of Management"),
    ("PES University", "PES Institute of Technology"),
    ("RV College of Engineering", "RV Institute of Management"),
    ("Government Engineering College Hassan", "Government Engineering College Ramanagara"),
    ("Sri Jayachamarajendra College of Engineering", "Sri Siddhartha Institute of Technology"),
]


def test_variants_of_same_college_merge() -> None:
    for group in SAME_COLLEGE:
        keys = {normalize_name(v) for v in group}
        assert len(keys) == 1, f"failed to merge {group} -> {keys}"


def test_different_colleges_never_collide() -> None:
    """The expensive failure: merging two colleges silently loses a lead."""
    for left, right in DIFFERENT_COLLEGES:
        assert normalize_name(left) != normalize_name(right), (
            f"collision: {left!r} and {right!r} both -> {normalize_name(left)!r}"
        )


def test_suffix_stripping_never_empties_a_name() -> None:
    """An acronym-only name must keep its suffix rather than reduce to nothing."""
    for name in ["BMS College of Engineering", "RV College of Engineering",
                 "College of Engineering", "Engineering College"]:
        assert normalize_name(name), f"{name!r} normalized away entirely"


def test_initialisms_collapse() -> None:
    assert normalize_name("R.V.") == normalize_name("RV") == normalize_name("R V")
    assert normalize_name("B M S Institute") == normalize_name("BMS Institute")


def test_empty_and_degenerate_input() -> None:
    assert normalize_name("") == ""
    assert normalize_name("   ") == ""
    assert normalize_name("!!!") == ""


def test_dedupe_key_includes_district() -> None:
    """Same name in two districts is two colleges, not one."""
    a = dedupe_key("Government Engineering College", "Hassan")
    b = dedupe_key("Government Engineering College", "Ramanagara")
    assert a != b
    # District spelling normalizes too.
    assert dedupe_key("X College", "Bengaluru Urban") == dedupe_key("X College", "bengaluru urban")


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
