"""FALLBACK — breadth-first crawler over a college site.

STATUS: PER-SITE FALLBACK as of 2026-08-02. Not the default path.

The brief expected `/v1/crawl` to be unusable. Live testing shows that is only
sometimes true, so this is wired in per-site rather than globally:

    bmsce.ac.in    -> pages_crawled: 20  (hit the max_pages limit)
    nitte.edu.in   -> pages_crawled: 20  (hit the max_pages limit)
    rvce.edu.in    -> pages_crawled: 1   (seed only, 197ms — bot protection)
    sit.ac.in      -> HTTP 400 "Domain does not resolve"

So `/v1/crawl` DOES follow internal links on most sites and should stay the
primary crawl stage. What it needs is a guard: when a crawl comes back with
one page (or fails) on a site whose homepage clearly has internal links, this
BFS crawler takes over for that college only.

One real gotcha, and probably what the earlier hand-testing hit: `/v1/crawl`
is ASYNCHRONOUS. It replies {"status": "queued", "job_id": ...} and crawls
nothing inline — a caller reading that immediate response sees no pages and
would reasonably conclude the endpoint is broken. Results come from
GET /v1/jobs/{job_id}, and the payload field is `urls`, not `pages`.

This crawler fetches each page through `/v1/scrape` (1 credit, full HTML with
format="html"). Deliberately NOT `/v1/scrape/batch`, which the brief records as
returning page titles only.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urldefrag, urljoin, urlparse

from .ollagraph_client import OllagraphClient, OllagraphError

log = logging.getLogger(__name__)

#: URL path fragments that suggest a placement / TPO page. Crawl priority
#: ordering, highest signal first. Reference data, safe to keep here.
PLACEMENT_PATH_HINTS: tuple[str, ...] = (
    "training-and-placement",
    "training_placement",
    "placement-cell",
    "placementcell",
    "tpo",
    "placements",
    "placement",
    "career",
    "careers",
    "recruiters",
    "campus-recruitment",
    "training",
    "contact-us",
    "contact",
    "about",
)

#: Never follow these — file downloads and dead ends, not contact pages.
SKIP_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".jpg", ".jpeg", ".png", ".gif", ".svg",
    ".mp4", ".mp3", ".avi",
})

DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_PAGES = 12

_HREF = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"'#]+)""", re.IGNORECASE)


class CrawledPage:
    """One fetched page plus why the crawler thought it was worth fetching.

    Carries BOTH representations, because neither alone is sufficient —
    measured on live college sites (2026-08-02):

        reva.edu.in/contact-us   html: 0 emails, 5 phones
                                 markdown: 9 emails, 0 phones
        bmsce.ac.in/home/Contact html: 558 bytes, nothing
                                 markdown: 9817 bytes, 1 email

    Ollagraph's HTML conversion drops most rendered text on these sites while
    markdown keeps it; markdown in turn discards the `data-cfemail` attributes
    the Cloudflare decoder needs, and sometimes the phone numbers. Fetching
    both costs a second credit per page and is the difference between a
    college yielding contacts and yielding nothing.
    """

    __slots__ = ("url", "html", "markdown", "depth", "priority")

    def __init__(self, url: str, html: str, depth: int, priority: int,
                 markdown: str = "") -> None:
        self.url = url
        self.html = html
        self.markdown = markdown
        self.depth = depth
        self.priority = priority

    @property
    def text(self) -> str:
        """Everything worth running an extractor over."""
        return f"{self.markdown}\n{self.html}" if self.markdown else self.html

    def __repr__(self) -> str:
        return f"CrawledPage({self.url!r}, depth={self.depth}, priority={self.priority})"


def _same_site(candidate: str, base_domain: str) -> bool:
    host = urlparse(candidate).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    base = base_domain[4:] if base_domain.startswith("www.") else base_domain
    return host == base or host.endswith(f".{base}")


def link_priority(url: str) -> int:
    """How promising a URL looks for finding placement contacts.

    Higher is better; 0 means "not worth a credit". Ordering matters because
    the page budget is small — each fetch costs a credit, and a college site
    can have hundreds of pages.
    """
    path = urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return 0
    for index, hint in enumerate(PLACEMENT_PATH_HINTS):
        if hint in path:
            # Earlier hints are stronger signals; PLACEMENT_PATH_HINTS is
            # ordered highest-signal-first.
            return len(PLACEMENT_PATH_HINTS) - index
    return 0


def extract_links(html: str, base_url: str) -> list[str]:
    """Absolute, same-site, deduplicated links from a page."""
    base_domain = urlparse(base_url).netloc.lower()
    seen: dict[str, None] = {}
    for match in _HREF.finditer(html):
        href = match.group(1).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute, _ = urldefrag(urljoin(base_url, href))
        if absolute.startswith(("http://", "https://")) and _same_site(absolute, base_domain):
            seen.setdefault(absolute, None)
    return list(seen)


async def crawl_site(
    client: OllagraphClient,
    base_url: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[CrawledPage]:
    """BFS a college site for placement-related pages.

    Fetches the seed page, then follows only links whose path suggests
    placement/contact content, best-first. The budget is deliberately small:
    every page costs a credit, and beyond a dozen pages the yield drops sharply
    while the bill does not.

    Returns pages in fetch order (seed first), each with raw HTML so the
    Cloudflare decoder can run over it if needed.
    """
    pages: list[CrawledPage] = []
    visited: set[str] = set()

    try:
        seed_response = await client.scrape(base_url, format="html")
        seed_html = seed_response.get("content") or ""
    except OllagraphError as exc:
        log.warning("crawl seed %s failed: %s", base_url, exc)
        return pages

    # Markdown carries the rendered text that HTML conversion drops on many
    # college sites; HTML carries the links this crawler needs to follow. Both
    # are kept — see CrawledPage for the measurements behind this.
    seed_markdown = ""
    try:
        seed_markdown = (await client.scrape(base_url, format="markdown")).get("content") or ""
    except OllagraphError:
        pass

    visited.add(base_url.rstrip("/"))
    pages.append(CrawledPage(base_url, seed_html, depth=0, priority=99,
                             markdown=seed_markdown))

    if not seed_html or max_depth < 1:
        return pages

    # (priority, depth, url) — highest priority first, shallowest as tiebreak.
    frontier: list[tuple[int, int, str]] = []
    for link in extract_links(seed_html, base_url):
        priority = link_priority(link)
        if priority > 0 and link.rstrip("/") not in visited:
            frontier.append((priority, 1, link))
    frontier.sort(key=lambda item: (-item[0], item[1]))

    while frontier and len(pages) < max_pages:
        priority, depth, url = frontier.pop(0)
        normalized = url.rstrip("/")
        if normalized in visited:
            continue
        visited.add(normalized)

        try:
            response = await client.scrape(url, format="html")
        except OllagraphError as exc:
            log.debug("crawl page %s failed: %s", url, exc)
            continue

        html = response.get("content") or ""
        markdown = ""
        try:
            markdown = (await client.scrape(url, format="markdown")).get("content") or ""
        except OllagraphError:
            pass

        if not html and not markdown:
            continue
        pages.append(CrawledPage(url, html, depth=depth, priority=priority,
                                 markdown=markdown))

        if depth < max_depth:
            additions: list[tuple[int, int, str]] = []
            for link in extract_links(html, base_url):
                child_priority = link_priority(link)
                if child_priority > 0 and link.rstrip("/") not in visited:
                    additions.append((child_priority, depth + 1, link))
            if additions:
                frontier.extend(additions)
                frontier.sort(key=lambda item: (-item[0], item[1]))

    log.info("crawled %d pages from %s", len(pages), base_url)
    return pages
