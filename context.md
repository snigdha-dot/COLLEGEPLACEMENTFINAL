# context.md

## Purpose
Python-backed system that builds a master list of Engineering + BCA
colleges per Indian state, scrapes each for placement-cell contact
details (falling back to general college contact info when none exists),
and serves results through a Next.js UI for the marketing team to search,
filter, and export.

## Tech stack
- Backend: FastAPI (Python), asyncio + httpx for concurrent scraping
- Storage: SQLite via the **stdlib `sqlite3` module — no ORM**. The brief
  left this open; the schema is two flat tables with no relational
  complexity, so SQLAlchemy would be a dependency without a payoff.
  Parameterized queries only (AGENTS.md security rule) — `sqlite3`'s `?`
  placeholders satisfy this directly.
  Exported to Excel/CSV on demand.
- Scraping: Ollagraph API (scrape, crawl, extract/contacts, verify/email,
  gmaps actors, search) as the primary engine for every stage; custom
  discovery/crawl/decoder modules exist only as a fallback, wired in only
  where Ollagraph demonstrably underperforms
- Frontend: Next.js (App Router), sonner for toasts

## Current status
**Phases 0-7 built. Pipeline verified working end-to-end on Karnataka (2026-08-02).**

114 tests pass across 10 modules; none spend credits. ~350 credits used
in total; balance ~62,190 of the starting 62,500.

### Pipeline proof (REVA University, BMS College of Engineering)
Both went from "no contact details found" to real extracted contacts:

    REVA University      phone +91-8046966966, fallback admissions@reva.edu.in,
                         3 backup emails, 3 backup phones
    BMS College of Engg  fallback info@bmsce.ac.in, 2 backup phones

Neither yielded a placement-specific email, so both are correctly
"Needs Follow-up" rather than "Verified" — and neither reaches the
marketing export, since that requires both an email and a phone.

### What live testing established (do not re-derive)

| Endpoint | Result | Cost |
|---|---|---|
| `/health` | ok | free |
| `/v1/search` | works | 3 cr |
| `/v1/search` with `site:` | **502 every time** | — |
| `/v1/scrape` `format=markdown` | **where the emails are** | 1 cr |
| `/v1/scrape` `format=html` | phones + data-cfemail, but drops text | 1 cr |
| `/v1/crawl` | **async**: job_id, then GET /v1/jobs/{id} | 1 cr |
| `/v1/extract/contacts` | works; keys are `address` / `raw` | 1 cr |
| `/v1/verify/email` | works | 1 cr |
| `/v1/actors/gmaps/*` | **BLOCKED upstream** | 30 cr |

Four findings that cost real debugging time:

1. **`/v1/extract/contacts` keys are `address` and `raw`/`normalized`, not
   `value`.** Reading the wrong key silently discarded every contact and made
   the whole pipeline report "nothing found" while the API was returning nine
   addresses per page.
2. **Markdown and HTML each lose what the other keeps.** reva.edu.in/contact-us:
   html 0 emails / 5 phones, markdown 9 emails / 0 phones. Pages are fetched
   as both.
3. **`/v1/crawl` is asynchronous** and does follow links on ~63% of college
   sites (bmsce 20 pages, nitte 20, reva 25; rvce and bmsit seed-only). The
   brief's "crawl does not follow links" finding was most likely a caller
   reading the immediate queued response. The BFS crawler is wired as a
   per-site fallback for the ~37% that fail.
4. **gmaps is dead upstream** — Ollagraph's own Apify account has ~$0.27 left.
   Returns HTTP 200 with ok=false, charges 30 credits, refunds async. The
   pipeline disables it after the first failure per run rather than paying 30
   credits per college. Re-enable with `SEED_ENABLE_MAPS=1`.

### Seed list: Karnataka / Engineering = **84 colleges**
26 directory-only, 38 aggregator-only, 20 corroborated by both. Cached at
`backend/seed_lists/karnataka_engineering.csv`.

Sanity check passes (right order of magnitude, real names) but the list is
incomplete — Karnataka has 200+ AICTE engineering colleges. That is the
expected consequence of the Maps channel being dead.

### Data loading without scraping
`backend/import_data.py` populates the DB from an existing spreadsheet, which
is how the UI is usable while gmaps is blocked upstream. One file may mix
states and streams. Imported rows are never marked Verified (confidence 0),
never preset outreach_status, and are still subject to the completeness filter.
A later scrape fills their empty fields without overwriting anything.

### Not done
- Phase 8: full 84-college run has NOT been executed. Cost estimate ~30
  credits/college = ~2,500 credits.
- `gh auth login` is pending, so the GitHub repo does not exist and nothing
  is pushed. All 15 commits are local.
- Phase 9 (auth on the UI) is explicitly post-v1.


## Build order and progress
- [x] 0. Scaffolding — AGENTS.md, context.md, README, git, venv, deps, frontend
- [x] 1. DB schema + models; inert fallback stubs
- [x] 2. Master list builder — 3 channels; Karnataka = 84, sanity check passed
- [x] 3. Ollagraph-only pipeline, end-to-end on pilot
- [x] 4. Evaluate pilot stage-by-stage; wire fallbacks only where needed
- [x] 5. Excel/CSV export — marketing schema + completeness filter
- [x] 6. FastAPI endpoints, split marketing vs admin
- [x] 7. Next.js frontend: marketing + admin views
- [ ] 8. Full re-run on pilot, verify marketing export, then generalize
- [ ] 9. (post-v1) basic auth on the UI

## Project structure
Every file gets a one-line description. Keep in sync with the actual repo.

```
AGENTS.md                      — agent rules: git, scope, security, attribution
context.md                     — this file: purpose, stack, status, findings, file map
README.md                      — overview + setup + how to run
requirements.txt               — runtime deps (pytest is dev-only, not listed)
.env.example                   — backend env template; real .env is gitignored
backend/
├── main.py                    — FastAPI app: routers, CORS (fixed origin), schema init
├── import_data.py             — CLI: load a ready-made CSV/Excel dataset straight into
│                                the DB. Standalone — imports no pipeline module, writes
│                                through the same repository, so it cannot break a scrape.
│                                Completeness filter still applies to imported rows.
├── api/
│   ├── deps.py                — per-request DB conn; RateLimiter for scrape/seed/export
│   ├── colleges.py            — list/detail/edit; view=marketing|admin split
│   ├── scrape.py              — POST /api/seed/build, /api/scrape/run, per-college rescrape
│   ├── routes_export.py       — GET /api/export (xlsx|csv)
│   └── export.py              — builds the export frame; marketing schema only
├── db/
│   ├── session.py             — sqlite3 connect + ctx manager; WAL, busy_timeout
│   ├── models.py              — SCHEMA + the marketing projection. The internal/marketing
│   │                            boundary is defined HERE, once, not in the API layer.
│   └── repository.py          — upsert (never erases a good prior scrape, never resets
│                                outreach_status), marketing_rows/admin_rows, ORDER BY
│                                allow-list, parameterized queries only
├── scraper/
│   ├── ollagraph_client.py    — async client; credit ledger + HARD CAP that aborts;
│   │                            detects actor ok=false; async crawl + job polling
│   ├── seed_builder.py        — 3 channels: maps (flagged off), directory, aggregator
│   │                            (NAMES ONLY — contacts discarded at collection)
│   ├── site_discovery.py      — LIVE. Finds the official site by domain scoring, since
│   │                            aggregators outrank colleges in search results
│   ├── pipeline.py            — per-college orchestrator: discover → crawl → extract →
│   │                            score → verify → fallback → result
│   ├── crawler.py             — BFS crawler, wired as a PER-SITE fallback for the ~37%
│   │                            of sites where /v1/crawl returns only the seed page
│   ├── contact_extractor.py   — scores contacts (tpo@ > info@), normalizes Indian phones
│   ├── cloudflare_decoder.py  — decodes data-cfemail; runs on every page (local, free)
│   ├── normalize.py           — dedupe keys; conservative so two colleges never merge
│   └── discovery.py           — FALLBACK, INERT. DuckDuckGo path; holds BLOCKED_DOMAINS
├── tests/                     — 114 tests, none spend credits
├── reference/state_districts.json  — 36 states/UTs, 770 districts
└── seed_lists/                — cached master lists (gitignored)
frontend/
├── app/
│   ├── page.tsx               — MARKETING view: clean schema, no status/confidence ever
│   ├── admin/page.tsx         — QA view: full records, real errors, job controls
│   ├── colleges/[id]/page.tsx — detail, hand-edit, re-scrape (params is a Promise → use())
│   └── layout.tsx             — sonner Toaster; no next/font (avoids a build-time fetch)
├── components/                — CollegeTable (view prop), FilterBar, LoadingSpinner
└── lib/api.ts                 — typed fetch wrapper; backend URL from env, no secrets
```

**Next.js 16 caution:** route `params`/`searchParams` are Promises. Check
`node_modules/next/dist/docs/` before writing frontend code rather than relying
on recall — the scaffold ships its own AGENTS.md saying exactly this.

## Data schema (reference)

### Internal (SQLite — full record, admin/QA view only)
college_name, state, stream (Engineering/BCA), affiliation, website,
placement_officer_name, placement_email, placement_phone,
backup_emails_found, backup_phones_found, fallback_contact_email,
fallback_contact_phone, confidence_score, source_urls, email_verified,
last_scraped, status (Verified / Needs Follow-up / Failed),
outreach_status (New / Contacted / Responded)

### Marketing export (Excel + marketing UI view)
college_name, state, stream, affiliation, website, contact_person, email,
phone, all_emails_found, all_phones_found, outreach_status (blank)

Rules:
- `email`/`phone` = single best contact; placement preferred, fallback used
  if no placement contact exists.
- A row appears ONLY if both `email` and `phone` are non-empty. Incomplete
  rows stay internal for QA and never reach marketing.
- `status`, `last_scraped`, `confidence_score` never leave the internal
  schema under any circumstance.
- `outreach_status` ships blank — marketing fills it in, never the pipeline.

## Ollagraph notes
Docs: https://ollagraph.com/docs/

The brief listed three friction points from prior hand-testing. All were
checked on the pilot (2026-08-02); two did not hold as stated:

- `/v1/scrape/batch` returns page titles only — **not re-tested.** The
  pipeline uses per-page `/v1/scrape`, so this never came up. Left alone.
- `/v1/crawl` does not follow internal links — **partly wrong.** It is
  asynchronous, and a caller reading the immediate `{"status":"queued"}`
  response sees no pages, which is the likeliest source of the original
  finding. Polled properly it crawls 20-25 pages on most college sites, and
  returns seed-only on roughly a third. The BFS crawler is wired per-site for
  those, not globally.
- Ollagraph's scrape strips `data-cfemail` — **not the real problem.** The
  actual issue was subtler: `format="html"` drops most rendered page text
  (including nearly all emails) while `format="markdown"` keeps it but loses
  the attribute. Pages are fetched as both. The decoder runs on every page
  since it is local and free.

The failure that actually cost the most time was in our own code, not
Ollagraph's: `/v1/extract/contacts` returns `{"address": ...}` for emails and
`{"raw"/"normalized": ...}` for phones. Reading `.get("value")` discarded
every contact silently, and the pipeline reported "no contact details found"
for colleges whose pages the API had parsed correctly.
