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


def test_normalize_phone_rejects_impossible_leading_digit() -> None:
    """A 10-digit Indian number is either a mobile (starts 6-9) or an STD code
    plus number (codes start 2-8). Nothing valid starts with 0 or 1 once the
    trunk prefix is stripped, so those are placeholders."""
    assert normalize_phone("1234567890") == ""
    assert normalize_phone("0123456789") == ""


def test_normalize_phone_accepts_landline_with_std_code() -> None:
    """Bengaluru numbers are 80 + 8 digits and must not be mistaken for junk
    just because they do not start 6-9."""
    assert normalize_phone("8026622130") == "+91-8026622130"
    assert normalize_phone("+91-80-26622130") == "+91-8026622130"


def test_normalize_phone_rejects_run_together_digits() -> None:
    """REGRESSION: page text like "080-26622130 35" produced 802662213035,
    a 12-digit string that reached a pilot result as a phone number. Nobody
    can dial it, so it must not reach marketing as one."""
    assert normalize_phone("802662213035") == ""
    assert normalize_phone("98765432101") == ""


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


def test_extract_and_score_parses_real_api_response_shape() -> None:
    """REGRESSION: /v1/extract/contacts wraps results in objects keyed
    "address" and "raw"/"normalized" — NOT "value". Reading the wrong key
    silently discarded every contact, and three pilot colleges reported "no
    contact details found" while the API had returned nine addresses including
    the TPO's. This is the exact live payload shape (2026-08-02)."""
    response = {
        "emails": [
            {"address": "admissions@reva.edu.in", "is_noreply": False},
            {"address": "placement@reva.edu.in", "is_noreply": False},
            {"address": "info@reva.edu.in", "is_noreply": False},
        ],
        "phones": [
            {"raw": "+91-80-46966966", "normalized": "+918046966966",
             "is_e164_shape": True},
        ],
    }
    emails, phones = extract_and_score(
        response, page_url="https://www.reva.edu.in/placement",
        college_domain="reva.edu.in",
    )
    values = [e.value for e in emails]
    assert "placement@reva.edu.in" in values, "TPO address was dropped"
    assert emails[0].value == "placement@reva.edu.in", "placement must sort first"
    assert len(phones) == 1 and phones[0].value.endswith("8046966966")


def test_extract_and_score_handles_alternative_key_names() -> None:
    """Bare strings and other plausible key names still work, so a change in
    the response shape degrades rather than silently returning nothing."""
    for payload in (
        {"emails": ["tpo@x.ac.in"], "phones": ["9876543210"]},
        {"emails": [{"email": "tpo@x.ac.in"}], "phones": [{"number": "9876543210"}]},
        {"emails": [{"value": "tpo@x.ac.in"}], "phones": [{"value": "9876543210"}]},
    ):
        emails, phones = extract_and_score(payload)
        assert emails and emails[0].value == "tpo@x.ac.in", payload
        assert phones and phones[0].value == "+91-9876543210", payload


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
