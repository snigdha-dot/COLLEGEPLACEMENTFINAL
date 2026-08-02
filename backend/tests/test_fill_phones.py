"""Tests for the phone-fill tool's site verification and extraction.

Site verification is the safeguard that matters most here: a URL from a
spreadsheet can be dead, parked, or point at a different institution, and a
wrong phone number on a row marketing then calls is worse than a blank one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.fill_phones import (  # noqa: E402
    _phones_from_text,
    site_belongs_to_college,
)


def test_page_naming_the_college_verifies() -> None:
    assert site_belongs_to_college(
        "Welcome to Atria Institute of Technology, Bangalore. Affiliated to VTU.",
        "Atria Institute of Technology, Bangalore",
        "https://atria.edu.in",
    )


def test_acronym_only_page_verifies() -> None:
    """Acronym-named colleges often never spell the full name out."""
    assert site_belongs_to_college(
        "KSIT Bangalore — admissions open for 2026.",
        "Kammavari Sangha Institute of Technology",
        "http://www.ksit.ac.in",
    )


def test_domain_name_in_page_verifies() -> None:
    """Last-resort signal: a college site nearly always names itself."""
    assert site_belongs_to_college(
        "Contact rithassan for admissions.",
        "Some Renamed Institute",
        "https://rithassan.ac.in",
    )


def test_parked_or_unrelated_domain_fails() -> None:
    """The case this exists to catch."""
    for text in [
        "This domain is for sale. Buy now.",
        "404 Not Found",
        "Welcome to a completely different organisation.",
    ]:
        assert not site_belongs_to_college(
            text, "Adichunchanagiri Institute of Technology", "http://parked.example"
        ), text


def test_empty_page_fails_verification() -> None:
    """REGRESSION: HKBK returned 67 bytes — a title with no body. There was
    nothing to verify against and nothing to scrape, so it must not pass."""
    assert not site_belongs_to_college("", "HKBK College of Engineering", "https://hkbk.edu.in")
    assert not site_belongs_to_college(
        "Best Engineering College in Bangalore | B.Tech College in Bangalore",
        "HKBK College of Engineering, Bangalore",
        "https://www.hkbk.edu.in",
    )


def test_phones_extracted_across_indian_formats() -> None:
    """The same number is written every possible way on college sites."""
    cases = [
        ("Contact: +91 80 2846 7248", "+91-8028467248"),
        ("Phone: 080-28467248", "+91-8028467248"),
        ("Call (080) 2846 7248", "+91-8028467248"),
        ("Mobile: +91-98450 12345", "+91-9845012345"),
    ]
    for text, expected in cases:
        assert expected in _phones_from_text(text), f"{text!r} -> {_phones_from_text(text)}"


def test_years_fees_and_pincodes_are_not_phones() -> None:
    """College pages are full of numbers that are not phone numbers."""
    text = (
        "Established 1999. Admissions 2026-27. Fees Rs 150000 per year. "
        "PIN 560001. Accredited 2019-2020."
    )
    assert _phones_from_text(text) == [], _phones_from_text(text)


def test_multiple_numbers_preserve_document_order() -> None:
    """The first number found becomes the stored contact, so order matters."""
    found = _phones_from_text("Office: 080-28467248 | Principal: 9845012345")
    assert found == ["+91-8028467248", "+91-9845012345"]


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
