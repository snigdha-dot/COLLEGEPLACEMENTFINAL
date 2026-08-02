"""SQLite schema + the marketing/internal projection boundary.

Two tables:
  colleges    — the full internal record, one row per college per stream
  scrape_runs — job bookkeeping so a crashed run is resumable and the admin
                view can report what happened

The marketing projection lives here rather than in the API layer on purpose:
the rule that `status`, `last_scraped`, and `confidence_score` never leave the
internal schema is a data rule, so the column list is defined once, next to the
schema it projects from, and both the UI view and the Excel export read it from
here. See context.md "Data schema".
"""

from __future__ import annotations

import sqlite3

# --- Column sets -----------------------------------------------------------

#: Full internal record — admin/QA view only. Never sent to marketing.
INTERNAL_COLUMNS = (
    "id",
    "college_name",
    "state",
    "stream",
    "affiliation",
    "website",
    "placement_officer_name",
    "placement_email",
    "placement_phone",
    "backup_emails_found",
    "backup_phones_found",
    "fallback_contact_email",
    "fallback_contact_phone",
    "confidence_score",
    "source_urls",
    "email_verified",
    "last_scraped",
    "status",
    "outreach_status",
)

#: Exactly what marketing may see, in display order.
#:
#: contact_person was dropped (2026-08-02): the pipeline never populated
#: placement_officer_name — a named officer is rarely published on college
#: sites and was empty in all 581 rows — so the column was pure noise in the
#: UI and the export. The DB field is kept, because a scrape or a hand edit
#: may still fill it, and it can be reinstated here if it starts carrying data.
MARKETING_COLUMNS = (
    "college_name",
    "state",
    "stream",
    "affiliation",
    "website",
    "email",
    "phone",
    "all_emails_found",
    "all_phones_found",
    "outreach_status",
)

#: Columns that must never appear in a marketing payload, under any
#: circumstance. Asserted against in tests and in the export path.
INTERNAL_ONLY_COLUMNS = frozenset(
    {"status", "last_scraped", "confidence_score", "id", "source_urls", "email_verified"}
)

STATUS_VALUES = ("Verified", "Needs Follow-up", "Failed")
OUTREACH_VALUES = ("New", "Contacted", "Responded")
STREAM_VALUES = ("Engineering", "BCA")
RUN_STATUS_VALUES = ("running", "completed", "failed", "cancelled")


# --- Schema ----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS colleges (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    college_name            TEXT    NOT NULL,
    normalized_name         TEXT    NOT NULL,
    district                TEXT,
    state                   TEXT    NOT NULL,
    stream                  TEXT    NOT NULL CHECK (stream IN ('Engineering', 'BCA')),
    affiliation             TEXT,
    website                 TEXT,

    placement_officer_name  TEXT,
    placement_email         TEXT,
    placement_phone         TEXT,

    backup_emails_found     TEXT,
    backup_phones_found     TEXT,

    fallback_contact_email  TEXT,
    fallback_contact_phone  TEXT,

    confidence_score        INTEGER NOT NULL DEFAULT 0,
    source_urls             TEXT,
    email_verified          INTEGER NOT NULL DEFAULT 0 CHECK (email_verified IN (0, 1)),

    last_scraped            TEXT,
    status                  TEXT    NOT NULL DEFAULT 'Needs Follow-up'
                                    CHECK (status IN ('Verified', 'Needs Follow-up', 'Failed')),
    outreach_status         TEXT    NOT NULL DEFAULT 'New'
                                    CHECK (outreach_status IN ('New', 'Contacted', 'Responded')),

    created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT    NOT NULL DEFAULT (datetime('now')),

    -- Dedupe key from the seed builder: normalized name + district, per the
    -- merge rule in the brief. A college offering both streams is two rows.
    UNIQUE (normalized_name, district, stream)
);

CREATE INDEX IF NOT EXISTS idx_colleges_state_stream ON colleges (state, stream);
CREATE INDEX IF NOT EXISTS idx_colleges_status       ON colleges (status);
CREATE INDEX IF NOT EXISTS idx_colleges_outreach     ON colleges (outreach_status);
CREATE INDEX IF NOT EXISTS idx_colleges_name         ON colleges (college_name);

-- Partial index over exactly the marketing-visible set: both contact fields
-- present after the placement-preferred-else-fallback coalesce.
CREATE INDEX IF NOT EXISTS idx_colleges_marketing_ready ON colleges (state, stream)
    WHERE COALESCE(NULLIF(placement_email, ''), NULLIF(fallback_contact_email, '')) IS NOT NULL
      AND COALESCE(NULLIF(placement_phone, ''), NULLIF(fallback_contact_phone, '')) IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_colleges_updated_at
AFTER UPDATE ON colleges
FOR EACH ROW
BEGIN
    UPDATE colleges SET updated_at = datetime('now') WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS scrape_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    state           TEXT    NOT NULL,
    stream          TEXT    NOT NULL CHECK (stream IN ('Engineering', 'BCA')),
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    total_colleges  INTEGER NOT NULL DEFAULT 0,
    processed       INTEGER NOT NULL DEFAULT 0,
    succeeded       INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_state_stream ON scrape_runs (state, stream);
"""

# --- Marketing projection --------------------------------------------------

#: The single best contact: placement preferred, fallback only if absent.
#: NULLIF guards against empty strings being treated as present.
_BEST_EMAIL = "COALESCE(NULLIF(placement_email, ''), NULLIF(fallback_contact_email, ''))"
_BEST_PHONE = "COALESCE(NULLIF(placement_phone, ''), NULLIF(fallback_contact_phone, ''))"

#: WHERE clause enforcing the completeness filter: a row reaches marketing only
#: if BOTH email and phone resolve to something non-empty. Placement or
#: fallback, doesn't matter which — both fields are a hard requirement.
MARKETING_COMPLETENESS_FILTER = (
    f"{_BEST_EMAIL} IS NOT NULL AND {_BEST_PHONE} IS NOT NULL"
)

#: SELECT list projecting an internal row down to the marketing schema.
#: outreach_status ships blank — it is marketing's field to fill in, never
#: pre-populated by the pipeline, so the stored value is deliberately dropped.
MARKETING_SELECT = f"""
    college_name,
    state,
    stream,
    affiliation,
    website,
    {_BEST_EMAIL} AS email,
    {_BEST_PHONE} AS phone,
    backup_emails_found AS all_emails_found,
    backup_phones_found AS all_phones_found,
    '' AS outreach_status
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables, indexes, and triggers. Safe to call repeatedly."""
    conn.executescript(SCHEMA)
