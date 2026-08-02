"""Master list builder: produce the colleges to scrape for a state + stream.

There is no single reliable source, so the brief specifies two channels that
get merged and deduped:

  1. MAPS      — /v1/actors/gmaps/search per district. Gives name, address,
                 phone, and sometimes website. Preferred for phone/address when
                 both sources agree on a college.
  2. DIRECTORY — /v1/search for official directory pages (state DTE, AICTE,
                 affiliating university), then /v1/extract/tables on any that
                 look authoritative.
  3. AGGREGATOR — NAMES ONLY. Listing sites (collegedunia, shiksha, …) publish
                 usable college names but unreliable contact details. This
                 channel harvests names and DISCARDS any phone/email/website it
                 sees; the pipeline then finds each college's real site and
                 contacts itself. Added 2026-08-02 after both channels above
                 came up empty for Karnataka (see below).

STATUS (2026-08-02), from live testing against Karnataka:

  MAPS       — IMPLEMENTED BUT DISABLED. Ollagraph's gmaps actors are backed by
               Apify and their upstream account is out of usage credit: calls
               return HTTP 200 with ok=false and
               `not-enough-usage-to-run-paid-actor`, charging 30 credits each
               (refunded asynchronously). Set SEED_ENABLE_MAPS=1 once that
               quota is restored.
  DIRECTORY  — LIVE BUT LOW-YIELD. The most authoritative page /v1/search found
               for Karnataka (vtu.ac.in/affiliated-institute) contains zero
               <table> elements under both /v1/scrape and /v1/scrape/smart — it
               is an affiliation-process page, not a college directory. Kept
               because other states may genuinely publish tables, but it cannot
               be relied on alone.
  AGGREGATOR — LIVE, and currently the only channel producing names. Explicitly
               authorised as a NAME source only.

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


# --- channel 3: aggregators (names only) ------------------------------------

#: Aggregator pages worth harvesting names from. Deliberately a small, explicit
#: allow-list rather than "anything not authoritative" — an arbitrary page that
#: merely mentions colleges is not a list of them.
AGGREGATOR_HOSTS = (
    "collegedunia.com", "shiksha.com", "careers360.com", "getmyuni.com",
    "collegedekho.com", "targetstudy.com", "zollege.in", "collegesearch.in",
)

#: Badge/label text listing pages prepend to a name ("Featured RV University").
_BADGE_PREFIX = re.compile(
    r"^(featured|sponsored|promoted|new|popular|trending|verified|ad)\s+",
    re.IGNORECASE,
)

#: Trailing noise that aggregator listings append to college names.
_LISTING_NOISE = re.compile(
    r"\s*[-–—,:|]\s*(admission|fees?|cutoff|placement|ranking|review|course|"
    r"eligibility|\d{4})\b.*$",
    re.IGNORECASE,
)

#: A name must contain one of these to be a plausible institution. Note that
#: "engineering"/"technology" alone are NOT enough — see _CATEGORY_PATTERNS.
_INSTITUTION_WORDS = re.compile(
    r"\b(college|institute|university|school|academy|polytechnic|vidyalaya|"
    r"vidyapeeth|institution)\b",
    re.IGNORECASE,
)

#: Listing pages are dominated by navigation categories, site tools, and course
#: names that all contain institution words. Observed on a live Karnataka run
#: (2026-08-02): of 220 raw "names", the large majority were entries like
#: "Engineering Colleges in Pune", "KCET College Predictor", and "Civil
#: Engineering". These patterns reject that class of string.
_CATEGORY_PATTERNS = (
    # "Engineering Colleges in Karnataka", "Top Colleges in India".
    # PLURAL ONLY, and never before "of": "College of Engineering" is the most
    # common real Indian college naming pattern, and an earlier version of this
    # rule rejected "BMS College of Engineering" outright.
    re.compile(r"\bcolleges\s+(in|for|accepting|near)\b", re.IGNORECASE),
    re.compile(r"\b(universities|institutes|schools)\s+(in|for|near)\b", re.IGNORECASE),
    # Site tools and editorial
    re.compile(r"\b(predictors?|comparison|compare|ranking|rank\s+list|calculator|"
               r"counselling|counseling|brochure|application\s+form|news|article|"
               r"exams?|results?|syllabus|question\s+paper|mock\s+test|"
               r"admission\s+test|entrance\s+test|cut\s?off)\b", re.IGNORECASE),
    # Plural/aggregate headings rather than one institution
    re.compile(r"^\s*(top|best|all|list\s+of|popular|view\s+all|explore|browse|"
               r"more|other|related|similar)\b", re.IGNORECASE),
    # Degree/course names
    re.compile(r"^\s*(b\.?\s?tech|m\.?\s?tech|b\.?\s?e\b|m\.?\s?e\b|b\.?\s?sc|"
               r"m\.?\s?sc|bca|mca|mba|diploma|phd|certificate)\b", re.IGNORECASE),
    # Call-to-action phrases: "Write a college review", "Find your college"
    re.compile(r"^\s*(write|find|search|get|see|check|apply|download|read|view|"
               r"add|submit|claim|register|join|start|know|discover)\b",
               re.IGNORECASE),
)

#: A real college name carries a distinguishing proper noun ("R.V.", "Nitte",
#: "Dayananda Sagar") beyond the generic institution vocabulary. Without one,
#: "Engineering College" and "Government College" are categories, not colleges.
_GENERIC_TOKENS = frozenset({
    "college", "colleges", "institute", "institutes", "institution", "university",
    "universities", "school", "schools", "academy", "polytechnic", "of", "the",
    "and", "in", "for", "engineering", "technology", "technical", "science",
    "sciences", "management", "studies", "research", "education", "arts",
    "commerce", "computer", "applications", "application", "top", "best", "all",
    "list", "government", "private", "autonomous", "deemed", "affiliated",
    "india", "indian", "state", "national",
})


def clean_listing_name(raw: str) -> str:
    """Strip listing-page decoration from a college name.

    Aggregator headings look like "RV College of Engineering - Admission 2026,
    Fees, Cutoff" or "MSRIT Bangalore - Ramaiah Institute of Technology,
    Bangalore". Returns "" if what remains does not look like a specific
    institution, so navigation links, course names, and article titles are
    dropped rather than seeded.
    """
    name = re.sub(r"\s+", " ", raw).strip().strip("|-–—:,")
    name = _BADGE_PREFIX.sub("", name).strip()
    name = _LISTING_NOISE.sub("", name).strip()
    name = _strip_alias_prefix(name)
    name = re.sub(r"^\d+[\.\)]\s*", "", name)          # leading "12. "
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()  # trailing "(Bangalore)"

    if len(name) < 8 or not _INSTITUTION_WORDS.search(name):
        return ""
    if any(pattern.search(name) for pattern in _CATEGORY_PATTERNS):
        return ""
    if not normalize_name(name):
        return ""

    # Require something distinguishing, otherwise this is a category label
    # ("Engineering College") rather than a specific institution. An acronym
    # counts: "RV College of Engineering" and "BMS College of Engineering" are
    # real colleges whose only distinctive token is the initialism, and
    # rejecting them would silently drop exactly the leads this pipeline exists
    # to find.
    tokens = re.findall(r"[A-Za-z\.]+", name)
    distinctive = [
        t for t in tokens
        if t.lower().strip(".") not in _GENERIC_TOKENS and len(t.strip(".")) > 1
    ]
    if not distinctive:
        return ""

    return name


#: "MSRIT Bangalore - Ramaiah Institute of Technology, Bangalore" — listing
#: pages prepend a short alias to the full name. The full name after the dash
#: is the useful half; the alias duplicates it and confuses dedupe.
#: The alias half must NOT itself contain an institution word — otherwise
#: "RV College of Engineering - Fees" looks like alias + real name and the
#: actual college name gets stripped away.
_ALIAS_PREFIX = re.compile(
    r"^(?P<alias>[A-Z][A-Za-z\.]{1,12}(?:\s+[A-Z][a-z]+)?)\s*[-–—]\s*(?P<rest>.+)$"
)


def _strip_alias_prefix(name: str) -> str:
    """Drop a leading short alias when a fuller name follows a dash.

    "MSRIT Bangalore - Ramaiah Institute of Technology" -> the full name.
    Fires only when the prefix is a bare alias and the remainder is a longer,
    institution-shaped name.
    """
    match = _ALIAS_PREFIX.match(name)
    if not match:
        return name

    alias, rest = match.group("alias"), match.group("rest").strip()
    # If the alias half is itself a college name, this is not an alias pattern.
    if _INSTITUTION_WORDS.search(alias):
        return name
    # The remainder has to look like a real institution and be substantially
    # longer than what we are discarding.
    if len(rest) >= 12 and _INSTITUTION_WORDS.search(rest) and len(rest) > len(alias):
        return rest
    return name


def is_aggregator(url: str) -> bool:
    return any(host in url.lower() for host in AGGREGATOR_HOSTS)


#: Places outside India that appear in aggregator "related colleges" widgets.
#: Observed on the Karnataka run: Stanford, MIT, Brunel, University of Texas,
#: Edge Hill. A geographic check is preferable to a blocklist because it
#: generalizes, but these catch names carrying no Indian place at all.
_FOREIGN_MARKERS = re.compile(
    r"\b(massachusetts|stanford|harvard|oxford|cambridge university|brunel|"
    r"texas|california|london|toronto|melbourne|sydney|singapore|malaysia|"
    r"dubai|edge hill|nanyang|monash|leeds|manchester|birmingham|glasgow)\b",
    re.IGNORECASE,
)


def plausibly_in_state(name: str, state: str) -> bool:
    """Reject names that clearly belong to another state or country.

    Aggregator pages surround the real list with "related colleges" widgets
    from elsewhere. A name is accepted if it mentions the state or one of its
    districts, or mentions nowhere in particular (most college names carry no
    place at all — "Siddaganga Institute of Technology"). It is rejected only
    when it names a foreign place or a different Indian state.
    """
    lowered = name.lower()
    if _FOREIGN_MARKERS.search(lowered):
        return False

    try:
        districts = [d.lower() for d in _load_districts(state)]
    except ValueError:
        districts = []

    if state.lower() in lowered or any(d in lowered for d in districts):
        return True

    # Common city names that are not district names but are in-state.
    import json

    with DISTRICTS_FILE.open(encoding="utf-8") as handle:
        all_states = json.load(handle)
    for other_state, other_districts in all_states.items():
        if other_state.startswith("_") or other_state == state:
            continue
        if other_state.lower() in lowered:
            return False
        for district in other_districts:
            # Only reject on a distinctive district name, not short ambiguous ones.
            if len(district) > 6 and district.lower() in lowered:
                return False

    return True


async def discover_via_aggregators(
    client: OllagraphClient, state: str, stream: Stream, *, max_pages: int = 5,
) -> list[SeedCollege]:
    """Harvest college NAMES from listing sites.

    Contact details found here are deliberately discarded: aggregators carry
    stale and wrong phone/email data, and the whole point of the pipeline is to
    source contacts from each college's own site. Only the name (and district
    when present) is kept, so a bad aggregator record costs a wasted lookup
    rather than a wrong contact reaching marketing.
    """
    candidates: dict[str, None] = {}
    queries = [
        f"list of {stream} colleges in {state}",
        f"top {stream} colleges in {state} list",
    ]
    for query in queries:
        try:
            response = await client.search(query, limit=10)
        except OllagraphError as exc:
            log.warning("aggregator search failed for %r: %s", query, exc)
            continue
        for item in response.get("results") or []:
            url = (item.get("url") or "").strip()
            if url and is_aggregator(url):
                candidates.setdefault(url, None)

    if not candidates:
        log.warning("no aggregator listing pages found for %s %s", state, stream)
        return []

    log.info("aggregator channel: %d candidate pages", len(candidates))
    found: list[SeedCollege] = []
    seen: set[tuple[str, str]] = set()

    for url in list(candidates)[:max_pages]:
        try:
            page = await client.scrape(url, format="html")
        except OllagraphError as exc:
            log.warning("aggregator page %s failed: %s", url, exc)
            continue

        html = page.get("content") or ""
        if not html:
            continue

        for raw in _candidate_names_from_html(html):
            name = clean_listing_name(raw)
            if not name or not plausibly_in_state(name, state):
                continue
            college = SeedCollege(
                college_name=name, state=state, stream=stream,
                source="aggregator", source_urls=[url],
                # phone/address/website intentionally left empty — see docstring.
            )
            if college.key in seen:
                continue
            seen.add(college.key)
            found.append(college)

    log.info("aggregator channel: %d distinct names", len(found))
    return found


#: Headings and link text are where listing pages put college names.
_NAME_BEARING = re.compile(
    r"<(?:h[23]|a)[^>]*>(.*?)</(?:h[23]|a)>", re.IGNORECASE | re.DOTALL
)
_TAGS = re.compile(r"<[^>]+>")


def _candidate_names_from_html(html: str) -> list[str]:
    """Pull candidate name strings out of headings and links."""
    out: list[str] = []
    for match in _NAME_BEARING.finditer(html):
        text = _TAGS.sub(" ", match.group(1))
        text = (
            text.replace("&amp;", "&").replace("&nbsp;", " ")
            .replace("&#039;", "'").replace("&quot;", '"')
        )
        text = re.sub(r"\s+", " ", text).strip()
        if 8 <= len(text) <= 120:
            out.append(text)
    return out


# --- merge -----------------------------------------------------------------

def merge_and_dedupe(
    maps_results: list[SeedCollege],
    directory_results: list[SeedCollege],
    aggregator_results: list[SeedCollege] | None = None,
) -> list[SeedCollege]:
    """Merge all channels on (normalized name, district).

    Trust order is Maps > directory > aggregator. Maps wins for phone/address
    when both describe the same college, per the brief. The aggregator channel
    contributes names only and can never overwrite a contact field — it is the
    least trustworthy source and its contact data is discarded at the point of
    collection, not here.
    """
    merged: dict[tuple[str, str], SeedCollege] = {}
    #: name -> key, so a row with no district can still match one that has a
    #: district. Channels disagree on this: directory tables carry a district
    #: column, aggregator listings usually do not, and without this a college
    #: found by both lands in the seed list twice. Observed on the live
    #: Karnataka run: 104 rows containing 20 such duplicate pairs.
    by_name: dict[str, tuple[str, str]] = {}

    def _resolve(college: SeedCollege) -> SeedCollege | None:
        """Find an already-merged row for this college, if any."""
        name_key, district_key = college.key
        if college.key in merged:
            return merged[college.key]
        # Only match on name alone when one side is missing a district — two
        # colleges with the same name in DIFFERENT districts must stay apart.
        prior = by_name.get(name_key)
        if prior is not None:
            prior_district = prior[1]
            if not district_key or not prior_district:
                return merged.get(prior)
        return None

    def _register(college: SeedCollege) -> None:
        merged[college.key] = college
        name_key = college.key[0]
        # Prefer to remember the variant that HAS a district, so later
        # district-less rows resolve against it.
        prior = by_name.get(name_key)
        if prior is None or (not prior[1] and college.key[1]):
            by_name[name_key] = college.key

    for college in maps_results:
        existing = _resolve(college)
        if existing is None:
            _register(college)
        else:
            existing.phone = existing.phone or college.phone
            existing.address = existing.address or college.address
            existing.website = existing.website or college.website

    for college in directory_results:
        existing = _resolve(college)
        if existing is None:
            _register(college)
            continue
        # Maps entry already present: keep its phone/address, note the overlap.
        existing.affiliation = existing.affiliation or college.affiliation
        existing.district = existing.district or college.district
        existing.source_urls = list({*existing.source_urls, *college.source_urls})
        if "directory" not in existing.source:
            existing.source = f"{existing.source}+directory"

    for college in aggregator_results or []:
        existing = _resolve(college)
        if existing is None:
            _register(college)
            continue
        # Corroboration only. Deliberately touches no contact field.
        existing.source_urls = list({*existing.source_urls, *college.source_urls})
        if "aggregator" not in existing.source:
            existing.source = f"{existing.source}+aggregator"

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
    use_aggregators: bool = True,
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

        aggregator_results: list[SeedCollege] = []
        if use_aggregators:
            aggregator_results = await discover_via_aggregators(client, state, stream)
            meta["channels"].append(
                {"channel": "aggregator", "found": len(aggregator_results), "names_only": True}
            )

        colleges = merge_and_dedupe(maps_results, directory_results, aggregator_results)
        meta["count"] = len(colleges)
        meta["credits"] = client.ledger.summary()

        if colleges:
            meta["cache_file"] = str(write_cache(state, stream, colleges))
        return colleges, meta
    finally:
        if owns_client:
            await client.close()
