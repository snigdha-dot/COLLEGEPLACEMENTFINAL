"""Tests for the BFS crawler's link extraction and prioritisation.

crawl_site itself needs a client and is exercised live; these cover the pure
logic that decides which links are worth spending a credit on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.scraper.crawler import (  # noqa: E402
    PLACEMENT_PATH_HINTS,
    extract_links,
    link_priority,
)

BASE = "https://bmsce.ac.in/"


def test_extract_links_resolves_relative_urls() -> None:
    html = """
      <a href="/placement">Placements</a>
      <a href="contact-us">Contact</a>
      <a href="https://bmsce.ac.in/tpo">TPO</a>
    """
    links = extract_links(html, BASE)
    assert "https://bmsce.ac.in/placement" in links
    assert "https://bmsce.ac.in/contact-us" in links
    assert "https://bmsce.ac.in/tpo" in links


def test_extract_links_stays_on_site() -> None:
    """Following off-site links would spend credits crawling Facebook."""
    html = """
      <a href="/placement">ok</a>
      <a href="https://facebook.com/bmsce">no</a>
      <a href="https://collegedunia.com/bmsce">no</a>
    """
    links = extract_links(html, BASE)
    assert links == ["https://bmsce.ac.in/placement"]


def test_extract_links_allows_subdomains() -> None:
    html = '<a href="https://placement.bmsce.ac.in/">placement portal</a>'
    assert extract_links(html, BASE) == ["https://placement.bmsce.ac.in/"]


def test_extract_links_skips_non_http_schemes() -> None:
    html = """
      <a href="mailto:tpo@bmsce.ac.in">mail</a>
      <a href="tel:+919876543210">call</a>
      <a href="javascript:void(0)">js</a>
      <a href="/placement">real</a>
    """
    assert extract_links(html, BASE) == ["https://bmsce.ac.in/placement"]


def test_extract_links_dedupes_and_strips_fragments() -> None:
    html = """
      <a href="/placement">a</a>
      <a href="/placement#top">b</a>
      <a href="/placement">c</a>
    """
    assert extract_links(html, BASE) == ["https://bmsce.ac.in/placement"]


def test_placement_paths_outrank_generic_ones() -> None:
    placement = link_priority("https://x.ac.in/training-and-placement")
    about = link_priority("https://x.ac.in/about")
    assert placement > about > 0


def test_irrelevant_paths_score_zero() -> None:
    """Zero means "not worth a credit" — the budget is small."""
    for url in ["https://x.ac.in/library", "https://x.ac.in/hostel",
                "https://x.ac.in/alumni", "https://x.ac.in/research"]:
        assert link_priority(url) == 0, url


def test_file_downloads_score_zero() -> None:
    for url in ["https://x.ac.in/placement-brochure.pdf",
                "https://x.ac.in/placement/report.xlsx",
                "https://x.ac.in/tpo/photo.jpg"]:
        assert link_priority(url) == 0, url


def test_every_hint_is_reachable() -> None:
    """A hint that never scores above zero is dead configuration."""
    for hint in PLACEMENT_PATH_HINTS:
        assert link_priority(f"https://x.ac.in/{hint}") > 0, hint


def test_malformed_html_is_safe() -> None:
    for html in ["", "<a>no href</a>", "<a href=''></a>", "not html at all"]:
        assert extract_links(html, BASE) == []


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
