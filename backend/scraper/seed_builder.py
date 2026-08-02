"""Master list builder: produce the colleges to scrape for a state + stream.

There is no single reliable source, so the brief specifies two channels that
get merged and deduped:

  1. MAPS      — /v1/actors/gmaps/search per district. Gives name, address,
                 phone, and sometimes website. Preferred for phone/address when
                 both sources agree on a college.
  2. DIRECTORY — /v1/search for official directory pages (state DTE, AICTE,
                 affiliating university), then /v1/extract/tables on any that
                 look authoritative.

STATUS (2026-08-02): the Maps channel is IMPLEMENTED BUT DISABLED. Ollagraph's
gmaps actors are backed by Apify and their upstream account is out of usage
credit — calls return HTTP 200 with ok=false and `not-enough-usage-to-run-paid-
actor`, charging 30 credits each (refunded asynchronously). The channel is
written and ready; set `use_maps=True` (or SEED_ENABLE_MAPS=1) once Ollagraph
restores that quota. Until then the directory channel runs alone, and the
resulting list should be treated as incomplete.

Caching: results are written to backend/seed_lists/<state>_<stream>.csv with a
generation timestamp and reused for CACHE_TTL_DAYS unless force_refresh=True.
Every Ollagraph call is billed, so this is a real cost control.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from .normalize import dedupe_key, normalize_name
from .ollagraph_client import OllagraphClient, OllagraphError, UpstreamActorError

log = logging.getLogger(__name__)

Stream = Literal["Engineering", "BCA"]

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"
SEED_LIST_DIR = Path(__file__).resolve().parent.parent / "seed_lists"
DISTRICTS_FILE = REFERENCE_DIR / "state_districts.json"

CACHE_TTL_DAYS = 30

#: Aggregator/listing sites that are not authoritative directories. A page from
#: one of these may still be *useful*, but it is not treated as a source of
#: record. Shared with the discovery fallback's BLOCKED_DOMAINS in spirit; kept
#: separate because the criteria differ (there: "not a college's own site";
#: here: "not an official directory").
NON_AUTHORITATIVE = (
    "collegedunia", "shiksha", "careers360", "collegesearch", "getmyuni",
    "collegedekho", "targetstudy", "icbse", "edufever", "collegepravesh",
    "sarvgyan", "minglebox", "successcds", "aglasem", "zollege", "campusoption",
    "careermudhra", "justdial", "indiamart", "sulekha", "wikipedia", "quora",
    "youtube", "facebook", "linkedin", "bing.com/aclk",
)

#: Domains that are official sources of record on their own.
AUTHORITATIVE_DOMAINS = (
    "aicte-india.org", "aicte.gov.in", "vtu.ac.in", "dtekarnataka",
    "kea.kar.nic.in", "dte.kar.nic.in",
)

#: A .gov.in / .nic.in / .ac.in domain is only authoritative if the URL also
#: looks like a college listing. Without this, "karnataka.gov.in/index.php" and
#: "incredibleindia.gov.in/en/karnataka" (a tourism page) both scored as
#: authoritative — observed 2026-08-02.
_OFFICIAL_TLDS = (".gov.in", ".nic.in", ".ac.in", ".edu.in")
_LISTING_HINTS = (
    "college", "institut", "affiliat", "approved", "autonomous", "directory",
    "list", "polytechnic",
)


@dataclass
class SeedCollege:
    """One college as discovered by the seed builder, pre-scrape."""

    college_name: str
    state: str
    stream: str
    district: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    affiliation: str = ""
    source: str = ""           # "maps" | "directory" | "maps+directory"
    source_urls: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return dedupe_key(self.college_name, self.district)


# --- cache -----------------------------------------------------------------

def cache_path(state: str, stream: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{state}_{stream}".lower()).strip("_")
    return SEED_LIST_DIR / f"{slug}.csv"


def read_cache(state: str, stream: str, ttl_days: int = CACHE_TTL_DAYS) -> list[SeedCollege] | None:
    """Return cached seeds if present and fresh, else None."""
    path = cache_path(state, stream)
    if not path.exists():
        return None

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return None

    generated = rows[0].get("generated_at", "")
    try:
        stamp = datetime.fromisoformat(generated)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("cache %s has an unreadable timestamp; treating as stale", path.name)
        return None

    age = datetime.now(timezone.utc) - stamp
    if age > timedelta(days=ttl_days):
        log.info("cache %s is %d days old (ttl %d); refreshing", path.name, age.days, ttl_days)
        return None

    log.info("using cached seed list %s (%d rows, %d days old)", path.name, len(rows), age.days)
    return [
        SeedCollege(
            college_name=r["college_name"], state=r["state"], stream=r["stream"],
            district=r.get("district", ""), address=r.get("address", ""),
            phone=r.get("phone", ""), website=r.get("website", ""),
            affiliation=r.get("affiliation", ""), source=r.get("source", ""),
            source_urls=[u for u in (r.get("source_urls") or "").split("|") if u],
        )
        for r in rows
    ]


def write_cache(state: str, stream: str, colleges: Iterable[SeedCollege]) -> Path:
    SEED_LIST_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(state, stream)
    generated_at = datetime.now(timezone.utc).isoformat()

    fields = [
        "college_name", "state", "stream", "district", "address", "phone",
        "website", "affiliation", "source", "source_urls", "generated_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for college in colleges:
            row = asdict(college)
            row["source_urls"] = "|".join(college.source_urls)
            row["generated_at"] = generated_at
            writer.writerow(row)
    return path


# --- channel 1: maps (implemented, currently disabled upstream) -------------

def _load_districts(state: str) -> list[str]:
    import json

    with DISTRICTS_FILE.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if state not in data:
        known = ", ".join(sorted(k for k in data if not k.startswith("_")))
        raise ValueError(f"unknown state {state!r}. Known: {known}")
    return data[state]


async def discover_via_maps(
    client: OllagraphClient, state: str, stream: Stream, *, limit_per_district: int = 20,
) -> list[SeedCollege]:
    """One gmaps search per district in the state.

    DISABLED by default — see the module docstring. Returns [] and logs rather
    than raising if the upstream actor is unavailable, so a partial run still
    produces a directory-only list instead of failing outright.
    """
    districts = _load_districts(state)
    log.info("maps channel: %d districts in %s", len(districts), state)
    found: list[SeedCollege] = []

    for district in districts:
        query = f"{stream} colleges in {district}, {state}"
        try:
            response = await client.gmaps_search(
                query, location=f"{district}, {state}", limit=limit_per_district
            )
        except UpstreamActorError as exc:
            log.error(
                "maps channel unavailable (%s) — abandoning it for this run; "
                "the directory channel will run alone and the list will be incomplete",
                exc,
            )
            return found
        except OllagraphError as exc:
            log.warning("maps search failed for %s: %s", district, exc)
            continue

        for item in response.get("results") or []:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            found.append(SeedCollege(
                college_name=name, state=state, stream=stream, district=district,
                address=(item.get("address") or "").strip(),
                phone=(item.get("phone") or "").strip(),
                website=(item.get("website") or "").strip(),
                source="maps",
            ))
    return found


# --- channel 2: directory pages --------------------------------------------

def is_authoritative(url: str) -> bool:
    """Is this URL plausibly an official directory rather than an aggregator?

    Two tiers: known directory domains pass outright; a generic official TLD
    additionally has to look like a college listing, so a state government
    homepage or tourism page does not qualify.
    """
    lowered = url.lower()
    if any(bad in lowered for bad in NON_AUTHORITATIVE):
        return False
    if any(domain in lowered for domain in AUTHORITATIVE_DOMAINS):
        return True
    if any(tld in lowered for tld in _OFFICIAL_TLDS):
        return any(hint in lowered for hint in _LISTING_HINTS)
    return False


def directory_queries(state: str, stream: Stream) -> list[str]:
    """Search queries most likely to surface an official directory page.

    Deliberately no `site:` operator — /v1/search returned 502 on every attempt
    for queries containing it (observed 2026-08-02), while the same query
    without it succeeds. Authoritative sources are filtered for after the fact
    by is_authoritative() instead.
    """
    return [
        f"list of AICTE approved {stream} colleges in {state}",
        f"{state} DTE list of {stream} colleges official",
        f"{state} technical university affiliated {stream} colleges list",
    ]


async def discover_via_directory(
    client: OllagraphClient, state: str, stream: Stream, *, max_pages: int = 4,
) -> list[SeedCollege]:
    """Find directory pages, then pull college names out of their tables."""
    candidates: dict[str, None] = {}
    for query in directory_queries(state, stream):
        try:
            response = await client.search(query, limit=10)
        except OllagraphError as exc:
            log.warning("directory search failed for %r: %s", query, exc)
            continue
        for item in response.get("results") or []:
            url = (item.get("url") or "").strip()
            if url and is_authoritative(url):
                candidates.setdefault(url, None)

    if not candidates:
        log.warning(
            "no authoritative directory pages found for %s %s — every result looked "
            "like an aggregator", state, stream
        )
        return []

    log.info("directory channel: %d authoritative candidate pages", len(candidates))
    found: list[SeedCollege] = []

    for url in list(candidates)[:max_pages]:
        try:
            page = await client.scrape(url, format="html")
            html = page.get("content") or ""
            if not html:
                continue
            tables = await client.extract_tables(html, min_rows=3, min_columns=2)
        except OllagraphError as exc:
            log.warning("directory page %s failed: %s", url, exc)
            continue

        for college in _colleges_from_tables(tables, state, stream, url):
            found.append(college)

    return found


#: Header cells that identify the column holding a college's name.
_NAME_HEADERS = ("college", "institution", "institute", "name of")
_DISTRICT_HEADERS = ("district", "place", "location", "city")


def _colleges_from_tables(
    tables: dict[str, Any], state: str, stream: Stream, source_url: str,
) -> list[SeedCollege]:
    """Pull college rows out of an /v1/extract/tables response.

    Directory tables vary wildly, so this identifies the name column by header
    text and skips anything it cannot interpret rather than guessing — a wrong
    column produces garbage rows that look plausible.
    """
    out: list[SeedCollege] = []
    for table in tables.get("tables") or []:
        rows = table.get("rows") or []
        headers = [str(h).lower() for h in (table.get("headers") or [])]
        if not rows:
            continue

        name_idx = next(
            (i for i, h in enumerate(headers) if any(k in h for k in _NAME_HEADERS)), None
        )
        if name_idx is None:
            continue
        district_idx = next(
            (i for i, h in enumerate(headers) if any(k in h for k in _DISTRICT_HEADERS)), None
        )

        for row in rows:
            cells = row if isinstance(row, list) else list(row.values())
            if name_idx >= len(cells):
                continue
            name = str(cells[name_idx]).strip()
            # Skip header repeats, totals, and numbering artifacts.
            if len(name) < 6 or not normalize_name(name):
                continue
            district = ""
            if district_idx is not None and district_idx < len(cells):
                district = str(cells[district_idx]).strip()
            out.append(SeedCollege(
                college_name=name, state=state, stream=stream, district=district,
                source="directory", source_urls=[source_url],
            ))
    return out


# --- merge -----------------------------------------------------------------

def merge_and_dedupe(
    maps_results: list[SeedCollege], directory_results: list[SeedCollege],
) -> list[SeedCollege]:
    """Merge both channels on (normalized name, district).

    Maps wins for phone/address when both sources describe the same college,
    per the brief. Directory contributes affiliation and corroboration.
    """
    merged: dict[tuple[str, str], SeedCollege] = {}

    for college in maps_results:
        existing = merged.get(college.key)
        if existing is None:
            merged[college.key] = college
        else:
            existing.phone = existing.phone or college.phone
            existing.address = existing.address or college.address
            existing.website = existing.website or college.website

    for college in directory_results:
        existing = merged.get(college.key)
        if existing is None:
            merged[college.key] = college
            continue
        # Maps entry already present: keep its phone/address, note the overlap.
        existing.affiliation = existing.affiliation or college.affiliation
        existing.source_urls = list({*existing.source_urls, *college.source_urls})
        if "directory" not in existing.source:
            existing.source = f"{existing.source}+directory"

    return list(merged.values())


# --- entry point -----------------------------------------------------------

def maps_enabled(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    return os.getenv("SEED_ENABLE_MAPS", "0").strip().lower() in {"1", "true", "yes"}


async def build_seed_list(
    state: str,
    stream: Stream,
    *,
    force_refresh: bool = False,
    use_maps: bool | None = None,
    client: OllagraphClient | None = None,
) -> tuple[list[SeedCollege], dict[str, Any]]:
    """Build (or load from cache) the master list for a state + stream.

    Returns (colleges, metadata). Metadata records which channels actually ran
    and what they cost, so an incomplete list is visibly incomplete rather than
    quietly short.
    """
    if not force_refresh:
        cached = read_cache(state, stream)
        if cached is not None:
            return cached, {"source": "cache", "count": len(cached), "channels": []}

    owns_client = client is None
    client = client or OllagraphClient()
    meta: dict[str, Any] = {"source": "live", "channels": [], "maps_enabled": maps_enabled(use_maps)}

    try:
        maps_results: list[SeedCollege] = []
        if maps_enabled(use_maps):
            maps_results = await discover_via_maps(client, state, stream)
            meta["channels"].append({"channel": "maps", "found": len(maps_results)})
        else:
            log.info("maps channel disabled (upstream Apify quota); directory only")
            meta["channels"].append({"channel": "maps", "found": 0, "skipped": "disabled"})

        directory_results = await discover_via_directory(client, state, stream)
        meta["channels"].append({"channel": "directory", "found": len(directory_results)})

        colleges = merge_and_dedupe(maps_results, directory_results)
        meta["count"] = len(colleges)
        meta["credits"] = client.ledger.summary()

        if colleges:
            meta["cache_file"] = str(write_cache(state, stream, colleges))
        return colleges, meta
    finally:
        if owns_client:
            await client.close()
