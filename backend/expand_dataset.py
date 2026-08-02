"""Grow the college dataset toward a target row count.

Combines the pieces that already exist rather than adding a new pipeline:

  1. SEED    — build (or reuse the cache of) the master list for each
               state+stream, via seed_builder's aggregator + directory
               channels. The Maps channel stays disabled upstream.
  2. DEDUPE  — drop anything already in the DB, using the same normalized
               name + district key the rest of the system uses, so a college
               already present under a different spelling is not re-added.
  3. ENRICH  — for each genuinely new college: discover its official site,
               fetch its contact pages, extract and validate contacts.
  4. STORE   — write immediately, one college at a time, so a run that stops
               partway keeps everything it already found.

Stops as soon as the DB reaches --target, so it never spends more than the
goal requires.

Usage:
    python -m backend.expand_dataset --target 500 --dry-run
    python -m backend.expand_dataset --target 500
    python -m backend.expand_dataset --target 500 --credit-cap 3000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .db import repository as repo
from .db.session import get_conn
from .scraper.normalize import dedupe_key
from .scraper.ollagraph_client import (
    CreditCapExceeded,
    OllagraphClient,
    OllagraphError,
)
from .scraper.pipeline import process_college
from .scraper.seed_builder import SeedCollege, build_seed_list

log = logging.getLogger(__name__)

#: Every state+stream combination to draw from, in the order they are tried.
#: Interleaved by stream so a stall in one state's engineering list does not
#: starve the others.
TARGETS: tuple[tuple[str, str], ...] = (
    ("Karnataka", "Engineering"),
    ("Tamil Nadu", "Engineering"),
    ("Andhra Pradesh", "Engineering"),
    ("Karnataka", "BCA"),
    ("Tamil Nadu", "BCA"),
    ("Andhra Pradesh", "BCA"),
)


def existing_keys() -> set[str]:
    """Normalized names already in the DB, so nothing is added twice."""
    with get_conn() as conn:
        rows = repo.admin_rows(conn, limit=100000)
    return {dedupe_key(r["college_name"], r["district"] or "")[0] for r in rows}


def db_count() -> int:
    with get_conn() as conn:
        return len(repo.admin_rows(conn, limit=100000))


async def collect_candidates(
    client: OllagraphClient, *, force_refresh: bool = False,
) -> list[SeedCollege]:
    """Gather seed colleges across every state+stream, minus what we have."""
    have = existing_keys()
    candidates: list[SeedCollege] = []
    seen: set[str] = set()

    for state, stream in TARGETS:
        try:
            colleges, meta = await build_seed_list(
                state, stream, force_refresh=force_refresh, client=client,  # type: ignore[arg-type]
            )
        except OllagraphError as exc:
            log.warning("seed build failed for %s/%s: %s", state, stream, exc)
            continue

        fresh = 0
        for college in colleges:
            key = dedupe_key(college.college_name, college.district)[0]
            if not key or key in have or key in seen:
                continue
            seen.add(key)
            candidates.append(college)
            fresh += 1

        print(f"  {state:16} {stream:12} {len(colleges):4} seeded, {fresh:4} new "
              f"({meta.get('source', 'live')})")

    return candidates


def _store(result: Any) -> None:
    with get_conn() as conn:
        repo.upsert_college(conn, {
            "college_name": result.college_name,
            "state": result.state,
            "stream": result.stream,
            "district": result.district,
            "affiliation": result.affiliation,
            "website": result.website,
            "placement_officer_name": result.placement_officer_name,
            "placement_email": result.placement_email,
            "placement_phone": result.placement_phone,
            "backup_emails_found": result.backup_emails_found,
            "backup_phones_found": result.backup_phones_found,
            "fallback_contact_email": result.fallback_contact_email,
            "fallback_contact_phone": result.fallback_contact_phone,
            "confidence_score": result.confidence_score,
            "source_urls": result.source_urls,
            "email_verified": result.email_verified,
            "last_scraped": result.last_scraped,
            "status": result.status,
        })


async def run(
    *, target: int, dry_run: bool = False, concurrency: int = 3,
    credit_cap: float = 5000, force_refresh: bool = False,
) -> None:
    start = db_count()
    print(f"DB currently holds {start} colleges; target is {target}.\n")
    if start >= target:
        print("Target already met — nothing to do.")
        return

    async with OllagraphClient(
        concurrency=concurrency, credit_cap=credit_cap, max_retries=2
    ) as client:
        print("Seeding candidate colleges:")
        candidates = await collect_candidates(client, force_refresh=force_refresh)
        print(f"\n{len(candidates)} candidate colleges not already in the DB.")

        needed = target - start
        if not candidates:
            print("No new colleges found. The seed channels are exhausted for these "
                  "states — see the note in context.md about the Maps channel being "
                  "blocked upstream.")
            return
        if len(candidates) < needed:
            print(f"WARNING: only {len(candidates)} candidates available but "
                  f"{needed} more are needed to reach {target}. Will add what exists.")

        if dry_run:
            print("\nDry run — nothing scraped or written. Sample:")
            for college in candidates[:20]:
                print(f"   {college.college_name[:56]:58} {college.state}/{college.stream}")
            print(f"   ... and {max(0, len(candidates) - 20)} more")
            return

        print(f"\nEnriching up to {needed} colleges (stops as soon as the DB hits "
              f"{target}):\n")

        added = failed = 0
        semaphore = asyncio.Semaphore(concurrency)
        stop = asyncio.Event()

        async def _one(seed: SeedCollege) -> None:
            nonlocal added, failed
            if stop.is_set():
                return
            async with semaphore:
                if stop.is_set():
                    return
                try:
                    result = await process_college(client, seed)
                except CreditCapExceeded:
                    stop.set()
                    raise
                except Exception as exc:  # noqa: BLE001 — one failure must not end the run
                    log.debug("pipeline failed for %s: %s", seed.college_name, exc)
                    failed += 1
                    return

                # Store even a contactless result: it records that the college
                # exists and was attempted, and the admin view can show it.
                _store(result)
                added += 1
                has_contact = bool(
                    (result.placement_email or result.fallback_contact_email)
                    and (result.placement_phone or result.fallback_contact_phone)
                )
                mark = "OK " if has_contact else "   "
                print(f"  {mark}{result.college_name[:40]:42} {result.status:16} "
                      f"{(result.placement_email or result.fallback_contact_email or '-')[:30]}")

                if start + added >= target:
                    stop.set()

        try:
            await asyncio.gather(*(_one(c) for c in candidates[: needed + 40]))
        except CreditCapExceeded as exc:
            print(f"\nSTOPPED: {exc}")

        print(f"\nLEDGER: {client.ledger.summary()}")

    with get_conn() as conn:
        total = len(repo.admin_rows(conn, limit=100000))
        visible = len(repo.marketing_rows(conn, limit=100000))
    print(f"\nadded    : {added}   (failed: {failed})")
    print(f"DB total : {start} -> {total}   (target {target})")
    print(f"marketing: {visible}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=500,
                        help="stop once the DB holds this many colleges")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be added without scraping")
    parser.add_argument("--credit-cap", type=float, default=5000,
                        help="abort the run past this many credits")
    parser.add_argument("--force-refresh", action="store_true",
                        help="rebuild seed lists instead of using the 30-day cache")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    asyncio.run(run(
        target=args.target, dry_run=args.dry_run, credit_cap=args.credit_cap,
        force_refresh=args.force_refresh, concurrency=args.concurrency,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
