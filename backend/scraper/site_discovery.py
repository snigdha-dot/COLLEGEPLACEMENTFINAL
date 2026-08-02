"""Find a college's official website (Ollagraph primary path).

Distinct from scraper/discovery.py, which is the INERT DuckDuckGo fallback.
This module is the live one: it uses /v1/search (and gmaps when that upstream
is working again) and scores candidate URLs.

The scoring problem is specific: a search for "RV College of Engineering
Bangalore official website" returns collegedunia and shiksha above the actual
college in most cases, because aggregators out-SEO the institutions. So the
top result is usually wrong, and picking it would poison every downstream
stage. Domain filtering is what makes this work, not result rank.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .discovery import BLOCKED_DOMAINS
from .normalize import normalize_name
from .ollagraph_client import OllagraphClient, OllagraphError

log = logging.getLogger(__name__)

#: TLDs that signal an Indian educational institution.
_EDU_TLDS = (".ac.in", ".edu.in", ".edu", ".ernet.in")

#: Government/institute TLDs that are plausible but weaker signals.
_WEAK_TLDS = (".org.in", ".nic.in", ".gov.in", ".org", ".in", ".com", ".net")

#: Paths that indicate a listing page rather than a college's own site, even on
#: a domain that is not itself blocked.
_LISTING_PATH = re.compile(
    r"/(colleges?|institutes?|universit(y|ies)|listing|directory|search|compare)"
    r"[-/]", re.IGNORECASE
)

#: Words in a domain that are never part of a college's own hostname.
_AGGREGATOR_WORDS = (
    "college", "admission", "exam", "career", "study", "student", "edu-",
    "ranking", "review", "compare", "search", "list", "info", "guide",
)

MIN_ACCEPTABLE_SCORE = 40


@dataclass
class SiteCandidate:
    url: str
    domain: str
    score: int
    reasons: list[str]

    @property
    def acceptable(self) -> bool:
        return self.score >= MIN_ACCEPTABLE_SCORE


def _domain_of(url: str) -> str:
    try:
        host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_blocked(url: str) -> bool:
    """Is this a known aggregator/social/directory domain?"""
    domain = _domain_of(url)
    return any(domain == b or domain.endswith(f".{b}") for b in BLOCKED_DOMAINS)


def _acronym(name: str) -> str:
    """"R.V. College of Engineering" -> "rvce" — how colleges pick domains."""
    words = re.findall(r"[A-Za-z]+", name)
    skip = {"of", "the", "and", "for", "in", "at"}
    return "".join(w[0].lower() for w in words if w.lower() not in skip)


def score_candidate(url: str, college_name: str, *, title: str = "") -> SiteCandidate:
    """Score how likely a URL is the college's own official site.

    Name-to-domain matching is the strongest signal: colleges use their name or
    acronym as the hostname ("rvce.edu.in", "bmsce.ac.in"), while aggregators
    use generic words.
    """
    domain = _domain_of(url)
    candidate = SiteCandidate(url=url, domain=domain, score=0, reasons=[])

    if not domain:
        candidate.reasons.append("unparseable URL")
        return candidate

    if is_blocked(url):
        candidate.reasons.append("blocked aggregator domain")
        return candidate

    if any(word in domain for word in _AGGREGATOR_WORDS):
        # "collegedunia.com" style. Not fatal on its own — "rvcollege.edu.in"
        # is legitimate — so it is a penalty rather than a rejection.
        candidate.score -= 15
        candidate.reasons.append("aggregator-like domain word")

    if any(domain.endswith(tld) for tld in _EDU_TLDS):
        candidate.score += 40
        candidate.reasons.append("educational TLD")
    elif any(domain.endswith(tld) for tld in _WEAK_TLDS):
        candidate.score += 10
        candidate.reasons.append("generic TLD")

    host_letters = re.sub(r"[^a-z]", "", domain.split(".")[0])
    normalized = normalize_name(college_name)
    name_letters = re.sub(r"[^a-z]", "", normalized)
    acronym = _acronym(college_name)

    name_matched = False
    if name_letters and host_letters:
        significant = [w for w in normalized.split() if len(w) > 3]
        if any(word in host_letters for word in significant):
            candidate.score += 35
            candidate.reasons.append("college name in domain")
            name_matched = True
        elif len(acronym) >= 3 and acronym in host_letters:
            candidate.score += 30
            candidate.reasons.append("acronym in domain")
            name_matched = True

    path = urlparse(url if "://" in url else f"https://{url}").path
    if _LISTING_PATH.search(path):
        candidate.score -= 20
        candidate.reasons.append("listing-style path")

    # A bare domain root is what a college homepage looks like.
    if path in ("", "/"):
        candidate.score += 10
        candidate.reasons.append("root path")

    title_matched = False
    if title:
        title_normalized = normalize_name(title)
        significant = [w for w in normalize_name(college_name).split() if len(w) > 3]
        if significant and any(w in title_normalized for w in significant):
            candidate.score += 15
            candidate.reasons.append("college name in page title")
            title_matched = True

    # Nothing tied this domain to THIS college — an educational TLD and a root
    # path alone would otherwise clear the threshold and hand the pipeline a
    # different college's website, which then poisons every later stage. Cap it
    # below acceptance so the caller records a failure instead.
    if not name_matched and not title_matched:
        candidate.score = min(candidate.score, MIN_ACCEPTABLE_SCORE - 1)
        candidate.reasons.append("no name match — capped below threshold")

    candidate.score = max(0, min(100, candidate.score))
    return candidate


async def discover_site(
    client: OllagraphClient, college_name: str, state: str, district: str = "",
) -> SiteCandidate | None:
    """Find the official website for one college.

    Returns the best acceptable candidate, or None if nothing scored high
    enough — in which case the caller should record the college as Failed
    rather than scrape a wrong site.
    """
    place = district or state
    query = f"{college_name} {place} official website"

    try:
        response = await client.search(query, limit=10)
    except OllagraphError as exc:
        log.warning("site discovery search failed for %r: %s", college_name, exc)
        return None

    candidates: list[SiteCandidate] = []
    for item in response.get("results") or []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        candidates.append(
            score_candidate(url, college_name, title=(item.get("title") or ""))
        )

    if not candidates:
        return None

    candidates.sort(key=lambda c: -c.score)
    best = candidates[0]

    if not best.acceptable:
        log.info(
            "no confident site for %r (best: %s scored %d — %s)",
            college_name, best.domain, best.score, ", ".join(best.reasons),
        )
        return None

    return best
