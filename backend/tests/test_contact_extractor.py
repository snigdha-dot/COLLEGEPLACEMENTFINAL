"""Tests for contact scoring and phone normalization.

The scoring rules decide which address reaches marketing as *the* placement
contact, so the properties that matter are ordering (tpo@ must outrank info@)
and the band boundaries the pipeline branches on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.scraper.contact_extractor import (  # noqa: E402
    CONFIDENCE_PLACEMENT,
    extract_and_score,
    normalize_phone,
    score_email,
    score_phone,
)


def test_placement_addresses_outrank_generic_ones() -> None:
    placement = score_email("tpo@rvce.edu.in", college_domain="rvce.edu.in")
    generic = score_email("info@rvce.edu.in", college_domain="rvce.edu.in")
    assert placement.score > generic.score
    assert placement.is_placement
    assert generic.is_generic


def test_placement_localpart_variants_all_recognised() -> None:
    for local in ["tpo", "placement", "placements", "placement.cell",
                  "training.placement", "trainingandplacement", "spo",
                  "career", "careers", "cdc"]:
        contact = score_email(f"{local}@college.ac.in", college_domain="college.ac.in")
        assert contact.score >= CONFIDENCE_PLACEMENT, \
            f"{local}@ scored {contact.score}, below the placement threshold"


def test_junk_addresses_score_zero() -> None:
    for junk in ["noreply@x.ac.in", "no-reply@x.ac.in", "postmaster@x.ac.in",
                 "your.email@example.com", "test@test.com"]:
        assert score_email(junk).score == 0, f"{junk} should score 0"


def test_page_url_context_lifts_score() -> None:
    """The same address found on a placement page is more likely the TPO."""
    on_placement_page = score_email(
        "contact@college.ac.in", page_url="https://college.ac.in/placement-cell"
    )
    on_home_page = score_email(
        "contact@college.ac.in", page_url="https://college.ac.in/"
    )
    assert on_placement_page.score > on_home_page.score


def test_free_mail_host_penalised_but_not_rejected() -> None:
    """Smaller colleges genuinely use gmail for the placement cell."""
    contact = score_email("tpoxyzcollege@gmail.com")
    assert contact.score > 0
    assert "free mail host" in contact.reasons


def test_phone_on_placement_page_beats_bare_phone() -> None:
    on_page = score_phone("+91-9876543210", page_url="https://x.ac.in/tpo")
    bare = score_phone("+91-9876543210", page_url="https://x.ac.in/about")
    assert on_page.score > bare.score
    assert not bare.is_placement


def test_phone_needs_url_and_text_context_to_reach_placement_band() -> None:
    """A number carries no local-part, so URL context alone (60) stays below
    the 70 threshold — page wording has to corroborate it. This is deliberate:
    a reception number on a placement page should not be published to
    marketing as the TPO's direct line."""
    url_only = score_phone("+91-9876543210", page_url="https://x.ac.in/placement")
    assert not url_only.is_placement, "URL context alone should not confirm a phone"

    corroborated = score_phone(
        "+91-9876543210",
        page_url="https://x.ac.in/placement",
        page_text="Training and Placement Officer: contact the placement cell on",
    )
    assert corroborated.is_placement


def test_normalize_phone_accepts_valid_indian_numbers() -> None:
    cases = [
        ("9876543210", "+91-9876543210"),
        ("+91 98765 43210", "+91-9876543210"),
        ("+91-9876543210", "+91-9876543210"),
        ("09876543210", "+91-9876543210"),
        ("091-9876543210", "+91-9876543210"),
    ]
    for raw, expected in cases:
        assert normalize_phone(raw) == expected, f"{raw!r} -> {normalize_phone(raw)!r}"


def test_normalize_phone_rejects_non_numbers() -> None:
    """Extractors routinely mistake years, PIN codes, and fees for phones."""
    for junk in ["2026", "560001", "150000", "", "abc", "0000000000",
                 "1111111111", "123", "12345678901234567"]:
        assert normalize_phone(junk) == "", f"{junk!r} should be rejected"


def test_normalize_phone_rejects_impossible_mobile_prefix() -> None:
    """Indian mobiles start 6-9; a 10-digit number starting 1-5 is not one."""
    assert normalize_phone("1234567890") == ""
    assert normalize_phone("5876543210") == ""


def test_extract_and_score_sorts_by_confidence() -> None:
    response = {
        "emails": ["info@rvce.edu.in", "tpo@rvce.edu.in", "noreply@rvce.edu.in"],
        "phones": ["+91-9876543210", "2026"],
    }
    emails, phones = extract_and_score(
        response, page_url="https://rvce.edu.in/placement", college_domain="rvce.edu.in"
    )
    assert emails[0].value == "tpo@rvce.edu.in", "placement address must sort first"
    assert emails[-1].value == "noreply@rvce.edu.in"
    assert [p.value for p in phones] == ["+91-9876543210"], "junk phone not filtered"


def test_extract_and_score_handles_dict_shaped_values() -> None:
    """/v1/extract/contacts may return objects rather than bare strings."""
    response = {"emails": [{"value": "tpo@x.ac.in"}], "phones": [{"value": "9876543210"}]}
    emails, phones = extract_and_score(response)
    assert emails[0].value == "tpo@x.ac.in"
    assert phones[0].value == "+91-9876543210"


def test_extract_and_score_dedupes_and_merges_extra_emails() -> None:
    """Cloudflare-decoded addresses are scored identically to extracted ones."""
    response = {"emails": ["info@x.ac.in"], "phones": []}
    emails, _ = extract_and_score(
        response, extra_emails=["tpo@x.ac.in", "INFO@x.ac.in"], college_domain="x.ac.in"
    )
    values = [e.value for e in emails]
    assert values.count("info@x.ac.in") == 1, "case-variant duplicate not merged"
    assert "tpo@x.ac.in" in values
    assert emails[0].value == "tpo@x.ac.in"


def test_empty_response_is_safe() -> None:
    emails, phones = extract_and_score({})
    assert emails == [] and phones == []


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
