"""Tests for the Cloudflare email decoder (inert fallback, phase-4 candidate).

Round-trip based: encode_cfemail is the inverse transform, so correctness is
checked against it across key values rather than against hand-copied vectors.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.scraper.cloudflare_decoder import (  # noqa: E402
    decode_all,
    decode_cfemail,
    encode_cfemail,
)

ADDRESSES = [
    "tpo@nitk.ac.in",
    "placement@rvce.edu.in",
    "principal@bmsce.ac.in",
    "training.placement@some-college.org",
]
KEYS = [0x00, 0x2A, 0x5B, 0x7F, 0xFF]


def test_round_trip_across_keys() -> None:
    for addr in ADDRESSES:
        for key in KEYS:
            assert decode_cfemail(encode_cfemail(addr, key)) == addr, (addr, key)


def test_malformed_payloads_return_none() -> None:
    """Bad markup must not raise — one broken attribute shouldn't kill a page."""
    for bad in ["", "z", "zzzz", "abc", "ab", "00", "6e6f74616e656d61696c"]:
        assert decode_cfemail(bad) is None, bad


def test_decode_all_preserves_document_order() -> None:
    """First contact on a page is usually the primary one — order is signal."""
    html = (
        f'<span data-cfemail="{encode_cfemail("first@a.ac.in")}">x</span>'
        f'<a href="/cdn-cgi/l/email-protection#{encode_cfemail("second@a.ac.in")}">y</a>'
        f'<span data-cfemail="{encode_cfemail("third@a.ac.in", 0x5B)}">z</span>'
    )
    assert decode_all(html) == ["first@a.ac.in", "second@a.ac.in", "third@a.ac.in"]


def test_decode_all_dedupes_and_skips_malformed() -> None:
    dup = encode_cfemail("tpo@example.ac.in")
    html = (
        f'<a href="/cdn-cgi/l/email-protection#{dup}">a</a>'
        f'<span data-cfemail="{encode_cfemail("placement@example.ac.in", 0x5B)}">b</span>'
        f'<span data-cfemail="{dup}">duplicate</span>'
        '<span data-cfemail="zzzz">malformed</span>'
        '<span data-cfemail="abc">odd length</span>'
    )
    assert decode_all(html) == ["tpo@example.ac.in", "placement@example.ac.in"]


def test_decode_all_on_page_without_obfuscation() -> None:
    assert decode_all("<p>plain@visible.com</p>") == []
    assert decode_all("") == []


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
