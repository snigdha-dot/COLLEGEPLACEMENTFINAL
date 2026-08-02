"""FALLBACK — breadth-first crawler over a college site.

STATUS: INERT. Not wired into the pipeline. Do not import this from the main
path.

Ollagraph `/v1/crawl` is the primary crawl stage. This module is the reserve
implementation (the prior `human_crawl.py` logic), to be wired in only if the
pilot confirms Ollagraph's crawl is insufficient.

WIRE-IN TRIGGER (phase 4): /v1/crawl returns only the seed page and does not
follow internal links. Prior hand-testing saw exactly this, but the brief is
explicit — CONFIRM on the pilot, do not assume. Ollagraph may have changed.

If wired in: BFS the site, prioritising placement-related paths, and fetch each
candidate page through Ollagraph `/v1/scrape` (not `/v1/scrape/batch`, which
returned titles only).
"""

from __future__ import annotations

from .discovery import FallbackNotWired

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

DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_PAGES = 40


def crawl_site(base_url: str, max_depth: int = DEFAULT_MAX_DEPTH,
               max_pages: int = DEFAULT_MAX_PAGES) -> list[str]:
    """BFS a college site for placement-related pages.

    NOT IMPLEMENTED — inert by design. Port the prior human_crawl.py logic here
    only if phase 4 confirms /v1/crawl does not follow internal links.
    """
    raise FallbackNotWired(
        "crawl fallback is inert; Ollagraph /v1/crawl is the primary path. "
        "Confirm /v1/crawl's link-following behavior on the pilot before wiring this in."
    )
