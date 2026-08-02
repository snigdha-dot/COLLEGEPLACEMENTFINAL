"""Find missing phone numbers for colleges that already have an email.

Narrower and much cheaper than the full pipeline. These colleges already have
a website and an email from the imported dataset; only the phone is missing,
so there is no discovery stage and no site-wide crawl — just a handful of
targeted page fetches per college.

Each college goes through:

  1. VERIFY the stored website actually resolves and belongs to this college.
     A URL from a spreadsheet may be dead, redirected, or simply wrong, and
     scraping the wrong site would attach a stranger's phone number to a row
     that marketing then calls.
  2. FETCH the contact-bearing pages (contact-us, placement, the homepage).
  3. EXTRACT phones via /v1/extract/contacts plus the local regex fallback,
     then validate through the same normalizer the rest of the system uses.
  4. STORE only a dialable number, and only against a verified site.

Usage:
    python -m backend.fill_phones --limit 5 --dry-run
    python -m backend.fill_phones --limit 25
    python -m backend.fill_phones --state Karnataka
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .db import repository as repo
from .db.session import get_conn
from .scraper.contact_extractor import normalize_phone
from .scraper.normalize import normalize_name
from .scraper.ollagraph_client import (
    CreditCapExceeded,
    OllagraphClient,
    OllagraphError,
)

log = logging.getLogger(__name__)

#: Paths most likely to carry a phone number, cheapest-first. The homepage is
#: included because Indian college sites very often put the number in a header
#: or footer that appears on every page.
_CONTACT_PATHS = ("", "/contact-us", "/contact", "/placement", "/placements")

#: Cap per college. Each page costs a credit, and past the first few the
#: chance of finding a number nobody has already found is low.
MAX_PAGES_PER_COLLEGE = 4

#: Indian numbers are written with spaces and dashes in every arrangement
#: ("+91 80 2846 7248", "080-28467248", "+91-98450 12345"), so the pattern
#: allows separators between digit groups and lets normalize_phone judge the
#: result. Anchored on a non-digit boundary so it does not slice a longer
#: number in half.
_PHONE_IN_TEXT = re.compile(
    r"(?<![\d])"
    r"(?:\+?\s?91[\s\-]?)?"        # optional country code
    r"(?:\(?0?\d{2,5}\)?[\s\-]?)?"  # optional STD code, maybe bracketed
    r"\d{3,5}[\s\-]?\d{3,5}"        # the number itself, possibly split
    r"(?![\d])"
)


@dataclass
class FillResult:
    college_id: int
    college_name: str
    website: str
    site_verified: bool = False
    phone: str = ""
    extra_phones: list[str] = field(default_factory=list)
    pages_tried: int = 0
    note: str = ""

    @property
    def succeeded(self) -> bool:
        return bool(self.phone)


def _domain(url: str) -> str:
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def site_belongs_to_college(page_text: str, college_name: str, url: str) -> bool:
    """Does this page plausibly belong to this college?

    Checked against the fetched page rather than the URL alone, because a
    spreadsheet URL can be stale, redirected, or simply wrong. Requires either
    a distinctive word from the name or the college's acronym to appear in the
    page text — enough to catch a domain that has been parked, sold, or
    mistyped, without demanding an exact title match.
    """
    if not page_text:
        return False

    haystack = re.sub(r"\s+", " ", page_text[:20000]).lower()
    normalized = normalize_name(college_name)

    distinctive = [w for w in normalized.split() if len(w) > 4]
    if any(word in haystack for word in distinctive):
        return True

    # Acronym-named colleges ("BMSIT", "KSIT") rarely spell the full name out.
    words = re.findall(r"[A-Za-z]+", college_name)
    skip = {"of", "the", "and", "for", "in", "at", "college", "institute",
            "technology", "engineering", "university", "science"}
    acronym = "".join(w[0].lower() for w in words if w.lower() not in skip)
    if len(acronym) >= 3 and acronym in haystack:
        return True

    # Last resort: the domain's own name appearing in the page (a college site
    # nearly always mentions itself somewhere).
    host = _domain(url).split(".")[0]
    return len(host) >= 5 and host in haystack


def _phones_from_text(text: str) -> list[str]:
    found: list[str] = []
    for match in _PHONE_IN_TEXT.finditer(text):
        normalized = normalize_phone(match.group(0))
        if normalized:
            found.append(normalized)
    return list(dict.fromkeys(found))


async def fill_one(client: OllagraphClient, row: Any) -> FillResult:
    """Try to find a phone number for one college."""
    result = FillResult(
        college_id=row["id"],
        college_name=row["college_name"],
        website=(row["website"] or "").strip(),
    )
    if not result.website:
        result.note = "no website on record"
        return result

    base = result.website.rstrip("/")
    # Deduplicate while preserving order; a stored URL may already be a
    # contact page, in which case it is the best first try.
    candidates = list(dict.fromkeys([base] + [f"{base}{p}" for p in _CONTACT_PATHS if p]))

    phones: list[str] = []
    for url in candidates[:MAX_PAGES_PER_COLLEGE]:
        try:
            response = await client.scrape(url, format="markdown")
        except OllagraphError as exc:
            log.debug("scrape %s failed: %s", url, exc)
            continue

        text = response.get("content") or ""
        if not text:
            continue
        result.pages_tried += 1

        # Verify ownership once, on the first page that actually loads.
        if not result.site_verified:
            if site_belongs_to_college(text, result.college_name, result.website):
                result.site_verified = True
            else:
                result.note = "site did not identify as this college"
                return result

        try:
            extracted = await client.extract_contacts(text, include_socials=False)
            for item in extracted.get("phones") or []:
                raw = item.get("normalized") or item.get("raw") if isinstance(item, dict) else item
                if isinstance(raw, str):
                    normalized = normalize_phone(raw)
                    if normalized:
                        phones.append(normalized)
        except OllagraphError as exc:
            log.debug("extract_contacts failed for %s: %s", url, exc)

        # Local regex as a backstop — free, and catches numbers the extractor
        # formats in ways it does not return.
        phones.extend(_phones_from_text(text))

        phones = list(dict.fromkeys(phones))
        if phones:
            break

    if phones:
        result.phone = phones[0]
        result.extra_phones = phones[1:6]
        result.note = f"found on {result.pages_tried} page(s)"
    elif result.site_verified:
        result.note = f"site verified but no phone found ({result.pages_tried} pages)"
    elif not result.note:
        result.note = "site unreachable"
    return result


async def run(
    *, limit: int, state: str | None = None, dry_run: bool = False,
    concurrency: int = 3, credit_cap: float = 2000,
) -> list[FillResult]:
    with get_conn() as conn:
        rows = repo.admin_rows(conn, state=state, limit=100000)
        visible = {r["college_name"] for r in repo.marketing_rows(conn, limit=100000)}

    targets = [
        r for r in rows
        if r["college_name"] not in visible
        and (r["placement_email"] or r["fallback_contact_email"])
        and not (r["placement_phone"] or r["fallback_contact_phone"])
        and r["website"]
    ][:limit]

    if not targets:
        print("nothing to do — no rows are missing only a phone")
        return []

    print(f"{len(targets)} colleges to process"
          f"{' (dry run — nothing will be written)' if dry_run else ''}\n")

    results: list[FillResult] = []
    semaphore = asyncio.Semaphore(concurrency)

    # max_retries=1: a 500 from /v1/scrape here means the college's own site is
    # down, not a transient API blip. Retrying with backoff costs ~7s per dead
    # site and never succeeds — measured across a 5-college sample where 4 of
    # the 5 sites were unreachable.
    async with OllagraphClient(
        concurrency=concurrency, credit_cap=credit_cap, max_retries=1
    ) as client:
        async def _one(row: Any) -> None:
            async with semaphore:
                try:
                    result = await fill_one(client, row)
                except CreditCapExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001 — one failure must not stop the run
                    log.exception("fill failed for %s", row["college_name"])
                    result = FillResult(row["id"], row["college_name"],
                                        row["website"] or "", note=f"error: {exc}")
                results.append(result)

                mark = "OK " if result.succeeded else "   "
                verified = "verified" if result.site_verified else "UNVERIFIED"
                print(f"  {mark}{result.college_name[:38]:40} {verified:10} "
                      f"{result.phone or '-':16} {result.note[:40]}")

                if result.succeeded and not dry_run:
                    with get_conn() as conn:
                        # Written as the FALLBACK contact, not the placement
                        # contact: nothing here established that this number
                        # belongs to the placement cell specifically.
                        repo.update_college(conn, result.college_id, {
                            "fallback_contact_phone": result.phone,
                        })

        try:
            await asyncio.gather(*(_one(row) for row in targets))
        except CreditCapExceeded as exc:
            print(f"\nSTOPPED: {exc}")

        print(f"\nLEDGER: {client.ledger.summary()}")

    found = sum(1 for r in results if r.succeeded)
    unverified = sum(1 for r in results if not r.site_verified)
    print(f"\nphones found : {found}/{len(results)}")
    print(f"unverified sites (skipped, not scraped further): {unverified}")
    if not dry_run and found:
        print(f"written to DB: {found}")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10,
                        help="max colleges to process (default 10)")
    parser.add_argument("--state", default=None, help="restrict to one state")
    parser.add_argument("--dry-run", action="store_true",
                        help="report findings without writing to the DB")
    parser.add_argument("--credit-cap", type=float, default=2000,
                        help="abort the run past this many credits")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    asyncio.run(run(limit=args.limit, state=args.state, dry_run=args.dry_run,
                    credit_cap=args.credit_cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
