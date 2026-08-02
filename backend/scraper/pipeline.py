"""Per-college pipeline: discover -> crawl -> extract -> score -> verify -> store.

Stage order and fallback triggers follow the brief. What live testing changed
(2026-08-02):

  DISCOVERY  — /v1/search only. The gmaps half is unavailable (Ollagraph's
               upstream Apify quota), so a college with no confident web result
               is recorded Failed rather than guessed at.
  CRAWL      — /v1/crawl first (it works on ~63% of college sites once polled
               as the async job it is). The BFS crawler takes over per-site
               when crawl returns just the seed page.
  EXTRACT    — /v1/extract/contacts over each page's HTML, plus the Cloudflare
               decoder run locally on the same HTML. The decoder is cheap,
               offline, and catches addresses the extractor cannot see, so it
               runs always rather than only on a detected failure.
  VERIFY     — /v1/verify/email on the chosen placement address only. Verifying
               every address found would multiply the bill for little gain.
  FALLBACK   — general contact info when no placement contact exists, so a row
               is never left with nothing when the site had *something*.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .cloudflare_decoder import decode_all
from .contact_extractor import (
    CONFIDENCE_PLACEMENT,
    ScoredContact,
    extract_and_score,
)
from .crawler import CrawledPage, crawl_site, extract_links, link_priority
from .ollagraph_client import (
    OllagraphClient,
    OllagraphError,
    UpstreamActorError,
)
from .seed_builder import SeedCollege
from .site_discovery import discover_site

log = logging.getLogger(__name__)

#: Pages to feed the extractor per college. Each costs a credit, and placement
#: contacts are concentrated in the first few placement-looking pages.
MAX_EXTRACT_PAGES = 8


@dataclass
class CollegeResult:
    """Everything the pipeline learned about one college."""

    college_name: str
    state: str
    stream: str
    district: str = ""
    website: str = ""
    affiliation: str = ""

    placement_officer_name: str = ""
    placement_email: str = ""
    placement_phone: str = ""

    backup_emails_found: list[str] = field(default_factory=list)
    backup_phones_found: list[str] = field(default_factory=list)

    fallback_contact_email: str = ""
    fallback_contact_phone: str = ""

    confidence_score: int = 0
    source_urls: list[str] = field(default_factory=list)
    email_verified: bool = False

    last_scraped: str = ""
    status: str = "Failed"
    notes: str = ""

    @property
    def has_any_contact(self) -> bool:
        return bool(
            self.placement_email or self.placement_phone
            or self.fallback_contact_email or self.fallback_contact_phone
        )


#: Paths a college site very often uses for the pages we care about. Tried when
#: the crawl did not surface anything placement-shaped, since a direct hit is
#: worth one credit and guessing costs nothing when it 404s.
_GUESS_PATHS = (
    "/placement", "/placements", "/placement-cell", "/training-and-placement",
    "/contact", "/contact-us",
)


def _guessed_contact_urls(website: str) -> list[str]:
    base = website.rstrip("/")
    return [f"{base}{path}" for path in _GUESS_PATHS]


async def _fetch_page(
    client: OllagraphClient, url: str, priority: int = 0,
) -> CrawledPage | None:
    """Fetch one page as both markdown and HTML.

    Two credits per page, deliberately. Markdown carries the rendered text
    (where nearly all emails live); HTML carries phone numbers and the
    data-cfemail attributes the Cloudflare decoder needs. Fetching only one
    format made colleges come back with zero contacts — see CrawledPage.
    """
    markdown = ""
    html = ""

    try:
        response = await client.scrape(url, format="markdown")
        markdown = response.get("content") or ""
    except OllagraphError as exc:
        log.debug("markdown scrape %s failed: %s", url, exc)

    # Only spend the second credit if the page proved to exist.
    if markdown:
        try:
            response = await client.scrape(url, format="html")
            html = response.get("content") or ""
        except OllagraphError as exc:
            log.debug("html scrape %s failed: %s", url, exc)

    if not markdown and not html:
        return None
    return CrawledPage(url, html, depth=0, priority=priority, markdown=markdown)


async def _gather_pages(
    client: OllagraphClient, website: str,
) -> tuple[list[CrawledPage], str]:
    """Get candidate pages for a site. Returns (pages, which_path_was_used).

    Tries /v1/crawl first. It returns URLs but not page content, so the URLs
    are prioritised and the promising ones fetched. When it comes back with
    only the seed URL, the BFS crawler takes over for this site.
    """
    crawl_urls: list[str] = []
    try:
        result = await client.crawl(website, max_pages=25, depth=2,
                                    poll_interval=4, timeout=180)
        crawl_urls = [u for u in (result.get("urls") or []) if isinstance(u, str)]
    except OllagraphError as exc:
        log.info("crawl failed for %s (%s) — using BFS fallback", website, exc)

    if len(crawl_urls) <= 1:
        log.info("crawl returned %d url(s) for %s — using BFS fallback",
                 len(crawl_urls), website)
        return await crawl_site(client, website), "bfs-fallback"

    # Crawl gave us a site map; spend the page budget on the best candidates.
    #
    # Do NOT drop zero-priority URLs. Measured on reva.edu.in: of 25 crawled
    # URLs only ONE scored above zero, and the crawl had not surfaced the
    # placement page at all — filtering to priority>0 left almost nothing to
    # extract from and the college came back with no contacts. Ranking is
    # useful; discarding is not, when the budget is bigger than the shortlist.
    ranked = sorted(crawl_urls, key=lambda u: -link_priority(u))
    chosen: list[str] = []
    if website not in ranked:
        chosen.append(website)
    chosen.extend(ranked)

    # A page the crawl never listed is still worth trying: placement and
    # contact pages live at predictable paths and are exactly what we want.
    for guess in _guessed_contact_urls(website):
        if guess not in chosen:
            chosen.append(guess)

    pages: list[CrawledPage] = []
    attempts = 0
    # Guessed URLs often 404, so allow a few extra attempts beyond the page
    # budget rather than letting misses eat it.
    for url in chosen:
        if len(pages) >= MAX_EXTRACT_PAGES or attempts >= MAX_EXTRACT_PAGES + 4:
            break
        attempts += 1
        page = await _fetch_page(client, url, priority=link_priority(url))
        if page is not None:
            pages.append(page)

    if not pages:
        # Crawl listed URLs but none could be fetched — fall back rather than
        # give up, since the site clearly exists.
        return await crawl_site(client, website), "bfs-fallback-after-empty"

    return pages, "ollagraph-crawl"


async def _extract_from_pages(
    client: OllagraphClient, pages: list[CrawledPage], college_domain: str,
) -> tuple[list[ScoredContact], list[ScoredContact]]:
    """Run contact extraction over every crawled page and merge the results."""
    all_emails: dict[str, ScoredContact] = {}
    all_phones: dict[str, ScoredContact] = {}

    for page in pages:
        # Extract over BOTH representations: markdown holds the rendered text
        # where nearly all emails live, HTML holds phone numbers and cfemail
        # attributes. Passing only one loses contacts outright.
        try:
            response = await client.extract_contacts(page.text)
        except OllagraphError as exc:
            log.debug("extract_contacts failed for %s: %s", page.url, exc)
            response = {}

        # Cloudflare-obfuscated addresses are invisible to the extractor. The
        # decoder is local and free, so it always runs rather than waiting for
        # a detected miss. It needs raw HTML specifically.
        decoded = decode_all(page.html)
        if decoded:
            log.debug("cloudflare decoder recovered %d address(es) on %s",
                      len(decoded), page.url)

        emails, phones = extract_and_score(
            response,
            page_url=page.url,
            page_text=page.text,
            college_domain=college_domain,
            extra_emails=decoded,
        )

        # Keep the highest-scoring sighting of each contact.
        for contact in emails:
            existing = all_emails.get(contact.value)
            if existing is None or contact.score > existing.score:
                all_emails[contact.value] = contact
        for contact in phones:
            existing = all_phones.get(contact.value)
            if existing is None or contact.score > existing.score:
                all_phones[contact.value] = contact

    emails = sorted(all_emails.values(), key=lambda c: -c.score)
    phones = sorted(all_phones.values(), key=lambda c: -c.score)
    return emails, phones


#: Set once a gmaps call fails with an upstream quota error. The actor is
#: down for the whole run, not for one college, and each attempt costs 30
#: credits — 10x a search — so retrying it per college would dominate the bill
#: for nothing. Observed: 60 of 81 credits in a 3-college test run went to two
#: dead gmaps calls.
_maps_unavailable = False


async def _maps_fallback(client: OllagraphClient, result: CollegeResult) -> None:
    """Ask Google Maps for a phone when the site yielded none.

    Currently a no-op in practice: the gmaps actors are out of upstream quota.
    Written so it starts working the moment that is restored.
    """
    global _maps_unavailable
    if _maps_unavailable:
        return

    try:
        place = await client.gmaps_place(
            name=result.college_name, location=f"{result.district or result.state}, India"
        )
    except UpstreamActorError as exc:
        _maps_unavailable = True
        log.warning(
            "maps fallback unavailable (%s) — disabling it for the rest of this "
            "run rather than spending 30 credits per college on a dead actor", exc,
        )
        return
    except OllagraphError as exc:
        log.debug("maps fallback failed for %s: %s", result.college_name, exc)
        return

    data = place.get("result") or place
    phone = (data.get("phone") or "").strip()
    if phone and not result.fallback_contact_phone:
        result.fallback_contact_phone = phone
        result.notes = (result.notes + " phone from Google Maps.").strip()


async def process_college(
    client: OllagraphClient, seed: SeedCollege,
) -> CollegeResult:
    """Run the full pipeline for one college."""
    result = CollegeResult(
        college_name=seed.college_name,
        state=seed.state,
        stream=seed.stream,
        district=seed.district,
        affiliation=seed.affiliation,
        website=seed.website,
        last_scraped=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    # --- 1. discovery ------------------------------------------------------
    if not result.website:
        candidate = await discover_site(
            client, seed.college_name, seed.state, seed.district
        )
        if candidate is None:
            result.status = "Failed"
            result.notes = "no confident official website found"
            return result
        result.website = candidate.url
        result.confidence_score = candidate.score

    college_domain = urlparse(result.website).netloc.lower()
    college_domain = college_domain[4:] if college_domain.startswith("www.") else college_domain

    # --- 2. crawl ----------------------------------------------------------
    try:
        pages, crawl_path = await _gather_pages(client, result.website)
    except OllagraphError as exc:
        result.status = "Failed"
        result.notes = f"crawl failed: {exc}"
        return result

    if not pages:
        result.status = "Failed"
        result.notes = "site discovered but no page could be fetched"
        return result

    result.source_urls = [p.url for p in pages]
    result.notes = f"crawled {len(pages)} pages via {crawl_path}"

    # --- 3. extract + score ------------------------------------------------
    emails, phones = await _extract_from_pages(client, pages, college_domain)

    # --- 4. split placement vs fallback ------------------------------------
    placement_emails = [c for c in emails if c.score >= CONFIDENCE_PLACEMENT]
    placement_phones = [c for c in phones if c.score >= CONFIDENCE_PLACEMENT]

    if placement_emails:
        best = placement_emails[0]
        result.placement_email = best.value
        result.confidence_score = max(result.confidence_score, best.score)
    if placement_phones:
        result.placement_phone = placement_phones[0].value

    # Everything else is a backup option for marketing.
    result.backup_emails_found = [
        c.value for c in emails if c.value != result.placement_email
    ]
    result.backup_phones_found = [
        c.value for c in phones if c.value != result.placement_phone
    ]

    # Best non-placement contact becomes the fallback.
    if not result.placement_email and emails:
        result.fallback_contact_email = emails[0].value
    if not result.placement_phone and phones:
        result.fallback_contact_phone = phones[0].value

    # --- 5. maps fallback for a missing phone ------------------------------
    if not result.placement_phone and not result.fallback_contact_phone:
        await _maps_fallback(client, result)

    # --- 6. verify the chosen email ----------------------------------------
    chosen_email = result.placement_email or result.fallback_contact_email
    if chosen_email:
        try:
            verification = await client.verify_email(chosen_email)
            result.email_verified = bool(
                verification.get("deliverable")
                or verification.get("valid")
                or verification.get("status") in ("valid", "deliverable")
            )
        except OllagraphError as exc:
            log.debug("email verification failed for %s: %s", chosen_email, exc)

    # --- 7. status ---------------------------------------------------------
    if result.placement_email and result.placement_phone:
        result.status = "Verified"
    elif result.has_any_contact:
        result.status = "Needs Follow-up"
    else:
        result.status = "Failed"
        result.notes = (result.notes + " no contact details found anywhere.").strip()

    return result


async def process_colleges(
    client: OllagraphClient,
    seeds: list[SeedCollege],
    *,
    concurrency: int = 3,
    on_result: Any = None,
) -> list[CollegeResult]:
    """Run the pipeline across many colleges with bounded concurrency.

    `on_result` is called with each CollegeResult as it completes, so the
    caller can persist incrementally — a run that dies partway should not lose
    the colleges it already finished.
    """
    semaphore = asyncio.Semaphore(concurrency)
    results: list[CollegeResult] = []

    async def _one(seed: SeedCollege) -> None:
        async with semaphore:
            try:
                result = await process_college(client, seed)
            except Exception as exc:  # noqa: BLE001 - one college must not kill the run
                log.exception("pipeline crashed for %s", seed.college_name)
                result = CollegeResult(
                    college_name=seed.college_name, state=seed.state,
                    stream=seed.stream, district=seed.district,
                    status="Failed", notes=f"pipeline error: {exc}",
                    last_scraped=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
            results.append(result)
            if on_result is not None:
                on_result(result)

    await asyncio.gather(*(_one(seed) for seed in seeds))
    return results
