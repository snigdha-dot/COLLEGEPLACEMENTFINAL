"""Tests for official-website discovery scoring.

The property that matters: aggregators out-SEO colleges, so the top search
result is usually collegedunia rather than the college. Picking by rank would
poison every downstream stage, so domain scoring has to override rank.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.scraper.site_discovery import (  # noqa: E402
    MIN_ACCEPTABLE_SCORE,
    is_blocked,
    score_candidate,
)


def test_official_site_outranks_aggregator() -> None:
    """The real ordering problem: aggregators rank above the college."""
    official = score_candidate("https://rvce.edu.in/", "R.V. College of Engineering")
    aggregator = score_candidate(
        "https://collegedunia.com/college/rvce-bangalore",
        "R.V. College of Engineering",
    )
    assert official.score > aggregator.score
    assert official.acceptable
    assert not aggregator.acceptable


def test_blocked_domains_score_zero() -> None:
    for url in [
        "https://collegedunia.com/college/x",
        "https://www.shiksha.com/college/x",
        "https://careers360.com/college/x",
        "https://www.facebook.com/rvcebangalore",
        "https://en.wikipedia.org/wiki/RV_College",
        "https://www.justdial.com/x",
    ]:
        candidate = score_candidate(url, "R.V. College of Engineering")
        assert candidate.score == 0, f"{url} scored {candidate.score}"
        assert not candidate.acceptable


def test_is_blocked_matches_subdomains() -> None:
    assert is_blocked("https://www.collegedunia.com/x")
    assert is_blocked("https://m.facebook.com/x")
    assert not is_blocked("https://rvce.edu.in/")


def test_educational_tld_boosts_score() -> None:
    edu = score_candidate("https://bmsce.ac.in/", "BMS College of Engineering")
    com = score_candidate("https://bmsce.com/", "BMS College of Engineering")
    assert edu.score > com.score
    assert "educational TLD" in edu.reasons


def test_acronym_domain_recognised() -> None:
    """Colleges commonly use an acronym as the hostname."""
    candidate = score_candidate("https://rvce.edu.in/", "R.V. College of Engineering")
    assert candidate.acceptable
    assert any("acronym" in r or "name" in r for r in candidate.reasons)


def test_name_in_domain_recognised() -> None:
    candidate = score_candidate(
        "https://siddaganga.edu.in/", "Siddaganga Institute of Technology"
    )
    assert candidate.acceptable
    assert "college name in domain" in candidate.reasons


def test_listing_path_penalised() -> None:
    """A directory page on an unblocked domain is still not the college."""
    listing = score_candidate(
        "https://example.edu.in/colleges/list-of-engineering",
        "Some Institute of Technology",
    )
    root = score_candidate("https://example.edu.in/", "Some Institute of Technology")
    assert root.score > listing.score


def test_unrelated_domain_below_threshold() -> None:
    """A .edu.in that has nothing to do with this college must not be accepted
    just because the TLD is educational."""
    candidate = score_candidate("https://someotherplace.edu.in/", "Nitte Meenakshi Institute of Technology")
    assert candidate.score < MIN_ACCEPTABLE_SCORE, (
        f"unrelated domain scored {candidate.score} ({candidate.reasons})"
    )


def test_partial_acronym_does_not_match_a_different_institution() -> None:
    """REGRESSION: "IIT Madras (IITM)" matched iitk.ac.in — IIT Kanpur — and
    stored dora@iitk.ac.in for a college 2,000km away. An acronym must
    essentially BE the host, not merely appear inside it."""
    wrong = score_candidate(
        "https://www.iitk.ac.in/", "IIT Madras (IITM) - Indian Institute of Technology"
    )
    assert not wrong.acceptable, f"IIT Kanpur accepted for IIT Madras ({wrong.score})"

    right = score_candidate("https://www.iitm.ac.in/", "Indian Institute of Technology Madras")
    assert right.acceptable, "the correct IIT Madras domain must still pass"


def test_malformed_url_is_safe() -> None:
    for bad in ["", "not a url", "://broken"]:
        candidate = score_candidate(bad, "Some College")
        assert not candidate.acceptable


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
