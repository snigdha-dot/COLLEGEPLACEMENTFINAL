"""Contact extraction and confidence scoring.

Ollagraph's /v1/extract/contacts does the regex work (emails, phones, socials)
on a page blob we already fetched. What it cannot do is judge which of the
addresses on a page is actually the Training & Placement Officer, so that
judgement lives here.

Scoring exists because a college site typically exposes several addresses —
principal@, info@, admissions@, tpo@ — and marketing needs the placement one.
A wrong-but-plausible contact is worse than an obviously generic one, since
nobody will notice it is wrong until an email bounces or annoys a principal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Local-parts that are unambiguously the placement cell. Highest confidence.
_PLACEMENT_LOCALPARTS = (
    "tpo", "placement", "placements", "placementcell", "placement_cell",
    "trainingandplacement", "training_placement", "trainingplacement",
    "spo", "cdc", "careerdevelopment", "career_development", "career",
    "careers", "corporaterelations", "industryrelations",
)

#: Local-parts that are a college contact but not placement-specific.
_GENERIC_LOCALPARTS = (
    "info", "contact", "enquiry", "enquiries", "admin", "office", "mail",
    "reception", "help", "support", "webmaster", "admission", "admissions",
    "principal", "director", "registrar", "hod", "dean",
)

#: Addresses that are never a useful outreach target.
_JUNK_LOCALPARTS = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
    "abuse", "mailer-daemon", "example", "test", "sample", "your", "email",
)

#: Free mail hosts. A placement address on one of these is plausible for a
#: smaller college but scores lower than one on the college's own domain.
_FREE_MAIL_HOSTS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com",
    "rediffmail.com", "live.com", "aol.com", "protonmail.com", "ymail.com",
})

#: Placement-related words appearing near a contact on the page.
_CONTEXT_WORDS = re.compile(
    r"\b(training\s*(and|&)?\s*placement|placement\s*(cell|officer|coordinator)?|"
    r"t\.?p\.?o\.?|career\s*(development|guidance|cell)|campus\s*recruitment|"
    r"corporate\s*relations|industry\s*(relations|interaction))\b",
    re.IGNORECASE,
)

#: Indian phone numbers: optional +91, optional STD code, 6-11 digits.
_PHONE_CLEAN = re.compile(r"[^\d+]")

#: Dates and year ranges, which scraped pages produce in bulk. Matches
#: "24.07.2026", "22-07-2024", "2022-2023", "2018-19", "2017-2018".
_DATE_LIKE = re.compile(
    r"\b(?:"
    r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"      # 24.07.2026, 22-07-2024
    r"|\d{4}[-/]\d{2,4}"                     # 2022-2023, 2018-19
    r"|(?:19|20)\d{2}\s*[-–]\s*(?:19|20)?\d{2}"  # 2017 - 2018
    r")\b"
)

# Confidence bands (0-100). Thresholds are what the pipeline acts on:
#   >= 70  placement contact — goes in placement_email/placement_phone
#   40-69  probable, needs review — stored but flagged Needs Follow-up
#   <  40  generic/fallback — goes in fallback_contact_*
CONFIDENCE_PLACEMENT = 70
CONFIDENCE_PROBABLE = 40


@dataclass
class ScoredContact:
    value: str
    kind: str                    # "email" | "phone"
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    source_url: str = ""

    @property
    def is_placement(self) -> bool:
        return self.score >= CONFIDENCE_PLACEMENT

    @property
    def is_generic(self) -> bool:
        return self.score < CONFIDENCE_PROBABLE


def _localpart(email: str) -> str:
    return email.split("@", 1)[0].lower()


def _domain(email: str) -> str:
    return email.split("@", 1)[-1].lower() if "@" in email else ""


def _normalized_localpart(email: str) -> str:
    """Strip separators so "training.placement" matches "trainingplacement"."""
    return re.sub(r"[._\-]", "", _localpart(email))


def score_email(
    email: str, *, page_url: str = "", page_text: str = "", college_domain: str = "",
) -> ScoredContact:
    """Score how likely an address is the real placement contact.

    Signals, strongest first: an unambiguous placement local-part; a placement
    keyword in the URL of the page it was found on; placement wording nearby in
    the page text. Generic local-parts score low but are kept — they become the
    fallback contact when no placement address exists anywhere.
    """
    contact = ScoredContact(value=email, kind="email", source_url=page_url)
    local = _normalized_localpart(email)
    domain = _domain(email)

    if any(junk in local for junk in _JUNK_LOCALPARTS):
        contact.score = 0
        contact.reasons.append("junk local-part")
        return contact

    if any(local.startswith(p) or p in local for p in _PLACEMENT_LOCALPARTS):
        contact.score += 60
        contact.reasons.append("placement local-part")
    elif any(local.startswith(g) for g in _GENERIC_LOCALPARTS):
        contact.score += 10
        contact.reasons.append("generic local-part")
    else:
        # A personal address (firstname.lastname@) — could be the TPO
        # themselves, so it is worth more than info@ but needs corroboration.
        contact.score += 20
        contact.reasons.append("unrecognised local-part")

    lowered_url = page_url.lower()
    if any(hint in lowered_url for hint in ("placement", "tpo", "career", "training")):
        contact.score += 25
        contact.reasons.append("found on a placement page")

    if page_text and _CONTEXT_WORDS.search(page_text):
        contact.score += 10
        contact.reasons.append("placement wording nearby")

    if college_domain and domain.endswith(college_domain.lower()):
        contact.score += 10
        contact.reasons.append("college domain")
    elif domain in _FREE_MAIL_HOSTS:
        contact.score -= 10
        contact.reasons.append("free mail host")

    contact.score = max(0, min(100, contact.score))
    return contact


def score_phone(
    phone: str, *, page_url: str = "", page_text: str = "",
) -> ScoredContact:
    """Score a phone number. Weaker signals than email — a number carries no
    local-part, so only page context distinguishes a TPO line from reception."""
    contact = ScoredContact(value=phone, kind="phone", source_url=page_url)
    contact.score = 15
    contact.reasons.append("phone found")

    lowered_url = page_url.lower()
    if any(hint in lowered_url for hint in ("placement", "tpo", "career", "training")):
        contact.score += 45
        contact.reasons.append("found on a placement page")

    if page_text and _CONTEXT_WORDS.search(page_text):
        contact.score += 25
        contact.reasons.append("placement wording nearby")

    contact.score = max(0, min(100, contact.score))
    return contact


def normalize_phone(raw: str) -> str:
    """Canonicalise an Indian phone number, or "" if implausible.

    Indian mobiles are 10 digits starting 6-9; landlines are STD code plus
    6-8 digits. Anything outside 8-13 digits is almost always a year, a PIN
    code, or a fee figure that the extractor mistook for a number.
    """
    # Reject date-shaped input before the separators are stripped. Scraped
    # pages are full of dates, and once punctuation is removed "24.07.2026"
    # becomes 24072026, which is indistinguishable from a landline by length
    # alone. Seven such dates reached the marketing view as phone numbers.
    if _DATE_LIKE.search(raw):
        return ""

    cleaned = _PHONE_CLEAN.sub("", raw)
    digits = cleaned.lstrip("+")

    # Strip trunk and country prefixes in either order: "0", "91", "091".
    # Applied as a loop because both can be present ("091-9876543210").
    for _ in range(2):
        if digits.startswith("0") and len(digits) > 10:
            digits = digits[1:]
        elif digits.startswith("91") and len(digits) > 11:
            digits = digits[2:]
        else:
            break

    if not 8 <= len(digits) <= 12:
        return ""
    if len(set(digits)) <= 2:          # 0000000000, 1111111111
        return ""

    if len(digits) == 10:
        # Mobiles start 6-9. Landlines are an STD code (2-8; Bengaluru is 80,
        # Delhi 11) followed by the number. Nothing valid starts with 0 or 1
        # once the trunk prefix has been stripped, so those are junk —
        # "1234567890" is a placeholder, not a phone number.
        if digits[0] not in "23456789":
            return ""
        return f"+91-{digits}"

    # Anything longer than 10 digits should have been reduced by the prefix
    # stripping above. If it was not, the digits ran together — page text like
    # "080-26622130 35" yields 802662213035, which is not a number anyone can
    # dial and must not reach marketing as one.
    if len(digits) > 10:
        return ""

    # Fewer than 10 digits: a landline missing its STD code, and therefore not
    # dialable. Scraped pages produce a lot of 8-9 digit noise (IDs, fees,
    # partial numbers) that is indistinguishable from a truncated landline, and
    # publishing an undialable number to marketing is worse than publishing
    # none — someone wastes a call and the row looked complete.
    return ""


#: /v1/extract/contacts wraps each result in an object rather than returning
#: bare strings, and the key is NOT "value" — verified against a live response
#: (2026-08-02):
#:   emails: {"address": "placement@reva.edu.in", "is_noreply": false}
#:   phones: {"raw": "+91-80-46966966", "normalized": "+918046966966", ...}
#: Reading the wrong key silently discarded every contact and made three
#: colleges report "no contact details found" when the API had returned nine
#: addresses including the TPO's. Several key names are accepted so a change in
#: the response shape degrades rather than fails silently.
_EMAIL_KEYS = ("address", "email", "value")
_PHONE_KEYS = ("normalized", "raw", "number", "phone", "value")


def _unwrap(item: Any, keys: tuple[str, ...]) -> str | None:
    """Pull the contact string out of a bare string or a wrapper object."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def extract_and_score(
    ollagraph_contacts: dict[str, Any],
    *,
    page_url: str = "",
    page_text: str = "",
    college_domain: str = "",
    extra_emails: Iterable[str] = (),
) -> tuple[list[ScoredContact], list[ScoredContact]]:
    """Turn an /v1/extract/contacts response into scored, sorted contacts.

    `extra_emails` carries addresses recovered by the Cloudflare decoder when
    that fallback is wired in — they are scored identically, since where an
    address came from says nothing about whether it is the placement contact.

    Returns (emails, phones), each sorted by descending confidence.
    """
    raw_emails = list(ollagraph_contacts.get("emails") or [])
    raw_phones = list(ollagraph_contacts.get("phones") or [])

    seen_emails: dict[str, None] = {}
    for email in [*raw_emails, *extra_emails]:
        value = _unwrap(email, _EMAIL_KEYS)
        if isinstance(value, str) and "@" in value:
            seen_emails.setdefault(value.strip().lower(), None)

    emails = [
        score_email(e, page_url=page_url, page_text=page_text, college_domain=college_domain)
        for e in seen_emails
    ]

    seen_phones: dict[str, None] = {}
    for phone in raw_phones:
        value = _unwrap(phone, _PHONE_KEYS)
        if isinstance(value, str):
            normalized = normalize_phone(value)
            if normalized:
                seen_phones.setdefault(normalized, None)

    phones = [score_phone(p, page_url=page_url, page_text=page_text) for p in seen_phones]

    emails.sort(key=lambda c: -c.score)
    phones.sort(key=lambda c: -c.score)
    return emails, phones
