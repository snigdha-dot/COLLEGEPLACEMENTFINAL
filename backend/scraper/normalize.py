"""College-name normalization for seed-list dedupe.

The seed builder merges two sources (Google Maps and directory pages) that
spell the same college differently — "R.V. College of Engineering",
"RV College of Engineering, Bengaluru", "Rashtreeya Vidyalaya College of
Engineering". Dedupe is by normalized name + district, per the brief.

This is deliberately conservative. Over-normalizing merges two genuinely
different colleges into one row and silently loses a lead, which is worse than
leaving a near-duplicate for a human to spot. So: strip punctuation, expand a
small set of known abbreviations, drop trailing location/qualifier noise — but
never strip a distinguishing word.

Pure functions, no network. Unit-tested in tests/test_normalize.py.
"""

from __future__ import annotations

import re
import unicodedata

#: Common suffixes that carry no distinguishing information once the stream is
#: already known. Order matters — longest first, so "college of engineering and
#: technology" is consumed before "college of engineering".
_SUFFIX_PATTERNS = (
    r"college of engineering and technology",
    r"college of engineering & technology",
    r"institute of engineering and technology",
    r"institute of technology and science",
    r"college of engineering",
    r"institute of technology",
    r"school of engineering",
    r"engineering college",
    r"college of science",
    r"degree college",
    r"first grade college",
    r"autonomous",
    r"deemed to be university",
    r"deemed university",
)

#: Abbreviations expanded so "Inst." and "Institute" normalize alike. Applied as
#: whole words only.
_ABBREVIATIONS = {
    "inst": "institute",
    "instt": "institute",
    "engg": "engineering",
    "engnrg": "engineering",
    "tech": "technology",
    "technolgy": "technology",
    "clg": "college",
    "coll": "college",
    "univ": "university",
    "sci": "science",
    "mgmt": "management",
    "std": "studies",
    "&": "and",
}

#: Honorifics and titles that appear inconsistently across sources.
_NOISE_WORDS = frozenset({
    "sri", "shri", "shree", "sree", "smt", "dr", "prof", "late",
    "the", "of", "a", "an",
})

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def _collapse_initialisms(text: str) -> str:
    """Join runs of single letters: "r v" -> "rv", "b m s college" -> "bms college".

    Sources spell initialisms inconsistently ("R.V." vs "RV" vs "R V"), and
    punctuation stripping turns the first two into separated letters. Without
    this, obvious duplicates fail to merge.
    """
    return re.sub(
        r"\b(?:[a-z]\s+){1,5}[a-z]\b",
        lambda m: m.group(0).replace(" ", ""),
        text,
    )


#: Place names that are common college-name prefixes in Karnataka. A name
#: reduced to one of these alone is not distinctive: "Bangalore Institute of
#: Technology" must not normalize to "bangalore", which would collide with
#: every other Bangalore-named college.
_PLACE_WORDS = frozenset({
    "bangalore", "bengaluru", "mysore", "mysuru", "mangalore", "mangaluru",
    "belgaum", "belagavi", "hubli", "hubballi", "dharwad", "gulbarga",
    "kalaburagi", "davangere", "shimoga", "shivamogga", "tumkur", "tumakuru",
    "bellary", "ballari", "bijapur", "vijayapura", "udupi", "manipal",
    "hassan", "mandya", "kolar", "bidar", "raichur", "koppal", "gadag",
    "haveri", "chitradurga", "karnataka", "east", "west", "north", "south",
    "new", "central", "city", "rural", "urban",
})


def _significant_words(text: str) -> list[str]:
    """Words that actually distinguish one college from another.

    Excludes bare acronyms and place names: if stripping a suffix would reduce
    a name to just "bms" or just "bangalore", that suffix was carrying the
    distinction and must be kept.
    """
    return [
        w for w in text.split()
        if w not in _NOISE_WORDS and len(w) > 3 and w not in _PLACE_WORDS
    ]


def normalize_name(name: str) -> str:
    """Reduce a college name to a dedupe key.

    Lowercase, accent-folded, punctuation-stripped, abbreviations expanded,
    known suffixes and honorifics removed. Returns "" for input that
    normalizes away entirely (caller should treat that as un-dedupable and
    keep the raw name).

    >>> normalize_name("R.V. College of Engineering")
    'rv'
    >>> normalize_name("Sri Siddhartha Institute of Tech.")
    'siddhartha institute technology'
    """
    if not name:
        return ""

    text = _strip_accents(name).lower()

    # Drop anything after a comma — almost always a city/state qualifier
    # ("BMS College of Engineering, Bengaluru"). Keep the head.
    text = text.split(",")[0]

    # "&" must become "and" before punctuation stripping removes it.
    text = text.replace("&", " and ")
    text = _PUNCT.sub(" ", text)
    text = _WS.sub(" ", text).strip()

    words = [_ABBREVIATIONS.get(w, w) for w in text.split()]
    text = _collapse_initialisms(" ".join(words))

    # Strip suffixes one at a time, but never strip the last distinguishing
    # words: "BMS College of Engineering" and "BMS Institute of Technology" are
    # different colleges, and reducing both to "bms" merges two real leads.
    for pattern in _SUFFIX_PATTERNS:
        candidate = re.sub(rf"\b{pattern}\b", " ", text)
        if _significant_words(candidate):
            text = candidate

    words = [w for w in text.split() if w not in _NOISE_WORDS]
    result = _WS.sub(" ", " ".join(words)).strip()

    # Suffix removal can leave a dangling conjunction ("Ballari Institute of
    # Technology and Management" -> "ballari and management"). Drop leading and
    # trailing connectives so the key reads cleanly.
    result = re.sub(r"^(and|&)\s+|\s+(and|&)$", "", result).strip()
    result = re.sub(r"\s+and\s+", " ", result).strip()

    # Same guard after noise-word removal.
    return result if result else _WS.sub(" ", text).strip()


def dedupe_key(name: str, district: str | None) -> tuple[str, str]:
    """Full dedupe key: normalized name + normalized district.

    Matches the UNIQUE (normalized_name, district, stream) constraint in the
    colleges table.
    """
    norm_district = ""
    if district:
        norm_district = _WS.sub(" ", _PUNCT.sub(" ", _strip_accents(district).lower())).strip()
    return normalize_name(name), norm_district
