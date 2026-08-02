"""FALLBACK — decode Cloudflare-obfuscated email addresses.

STATUS: IMPLEMENTED BUT UNWIRED. Nothing in the main path calls this yet.

Ollagraph `/v1/extract/contacts` is the primary extraction stage. This module
is the reserve for one specific failure: Cloudflare's email-obfuscation, common
on the WordPress sites many Indian college sites run on. The address is stored
in a `data-cfemail` attribute and rendered by JS, so it is absent from the
static text an extractor sees.

WIRE-IN TRIGGER (phase 4): an email is visibly present on a page as
Cloudflare-obfuscated markup but does not appear in Ollagraph's extraction
output. Prior hand-testing found Ollagraph's scrape stripped the `data-cfemail`
attribute entirely — CONFIRM this on the pilot rather than assuming. If
confirmed, the fix is to fetch that page's raw HTML directly (bypassing
Ollagraph) and run `decode_all` over it.

Unlike the discovery and crawl fallbacks, the decode itself is a fixed,
well-specified transform with no network dependency, so it is implemented and
unit-tested here rather than left as a stub. Wiring remains a phase-4 decision.

The scheme: hex-encoded bytes where byte 0 is an XOR key and each subsequent
byte is a character of the address XORed with that key.
"""

from __future__ import annotations

import re

#: Matches both obfuscation forms in one pass so results come back in document
#: order — the first contact on a page is usually the primary one, so that
#: ordering is signal the confidence scorer can use:
#:   <span data-cfemail="abcdef...">
#:   <a href="/cdn-cgi/l/email-protection#abcdef...">
_CFEMAIL = re.compile(
    r'data-cfemail="([0-9a-fA-F]+)"'
    r'|/cdn-cgi/l/email-protection#([0-9a-fA-F]+)'
)


def decode_cfemail(encoded: str) -> str | None:
    """Decode one `data-cfemail` hex string to an email address.

    Returns None if the payload is malformed rather than raising — a single bad
    attribute on a page should not abort extraction for that page.
    """
    if len(encoded) < 4 or len(encoded) % 2:
        return None
    try:
        data = bytes.fromhex(encoded)
    except ValueError:
        return None

    key = data[0]
    try:
        decoded = "".join(chr(b ^ key) for b in data[1:])
    except ValueError:
        return None

    # Cheapest possible sanity check; full validation belongs to /v1/verify/email.
    if "@" not in decoded or "." not in decoded.split("@")[-1]:
        return None
    return decoded


def decode_all(html: str) -> list[str]:
    """Find and decode every Cloudflare-obfuscated address in raw HTML.

    Requires RAW html — Ollagraph's scrape output may have stripped the
    attribute this depends on. Deduplicated, order preserved.
    """
    seen: dict[str, None] = {}
    for match in _CFEMAIL.finditer(html):
        decoded = decode_cfemail(match.group(1) or match.group(2))
        if decoded:
            seen.setdefault(decoded, None)
    return list(seen)


def encode_cfemail(email: str, key: int = 0x2a) -> str:
    """Inverse of decode_cfemail. Test helper only — never used in the pipeline."""
    if not 0 <= key <= 0xFF:
        raise ValueError("key must be a single byte")
    return format(key, "02x") + "".join(format(ord(c) ^ key, "02x") for c in email)
