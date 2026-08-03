"""FALLBACK — website discovery via DuckDuckGo + domain scoring.

STATUS: INERT. Not wired into the pipeline. Do not import this from the main
path.

Ollagraph (`/v1/search` + `/v1/actors/gmaps/search`) is the primary discovery
channel. This module exists only for the case where that demonstrably
underperforms on the pilot state — per the brief, fallbacks get wired in
per-stage, only after evidence, never speculatively.

WIRE-IN TRIGGER (phase 4): neither /v1/search nor gmaps returns a confident
match, or the top result is a directory/aggregator rather than the college's
real site.

The prior hand-validated implementation used a blocked-domain list plus a
scoring heuristic. BLOCKED_DOMAINS below is that list, kept because it is
reference data rather than logic. The scoring function is left unimplemented
on purpose: it should be ported only if phase 4 shows it is needed.
"""

from __future__ import annotations

#: Aggregators, directories, and listing sites that are never a college's
#: official website. Ported from the prior validated implementation.
BLOCKED_DOMAINS: frozenset[str] = frozenset({
    "collegedunia.com",
    "shiksha.com",
    "careers360.com",
    "collegesearch.in",
    "getmyuni.com",
    "collegedekho.com",
    "indiacollegesearch.com",
    "targetstudy.com",
    "icbse.com",
    "edufever.com",
    "collegepravesh.com",
    "sarvgyan.com",
    "minglebox.com",
    "successcds.net",
    "aglasem.com",
    "justdial.com",
    "indiamart.com",
    "sulekha.com",
    "facebook.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "wikipedia.org",
    "quora.com",
    "indeed.com",
    "naukri.com",
    "glassdoor.co.in",
    # Job boards that match on institution words. governmentjobs.com was
    # picked as the website for three separate "Government ..." colleges, which
    # then all received support@governmentjobs.com as their contact.
    "governmentjobs.com",
    "monster.com",
    "shine.com",
    "timesjobs.com",
    "freshersworld.com",
})


class FallbackNotWired(RuntimeError):
    """Raised if an inert fallback is invoked before phase 4 evaluates it."""


def discover_website(college_name: str, district: str, state: str) -> str | None:
    """Find a college's official website via DuckDuckGo + scoring.

    NOT IMPLEMENTED — inert by design. Wire in only if phase 4 shows Ollagraph
    discovery underperforming, then port the scoring heuristic from the prior
    implementation.
    """
    raise FallbackNotWired(
        "discovery fallback is inert; Ollagraph /v1/search is the primary path. "
        "Wire this in only after phase 4 shows it is needed."
    )
