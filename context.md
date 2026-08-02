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
**Phase 1 complete — DB schema + inert fallback stubs (2026-08-02).**

Pilot state for phases 2–4: **Karnataka** (chosen because ~437 colleges
across KA/AP/TN were hand-validated in prior testing, giving a ground-truth
count to sanity-check the seed builder against).

Nothing is wired to Ollagraph yet. **No API calls have been made and nothing
has been billed.** The three fallback modules exist but are deliberately
unwired — discovery and crawler raise `FallbackNotWired` if called; the
Cloudflare decoder is fully implemented and unit-tested but nothing calls it.

**BLOCKED at phase 2.** `.env` does not exist and `OLLAGRAPH_API_KEY` is not
set. Every remaining phase is Ollagraph-dependent, so the pipeline cannot run
until the maintainer creates `.env` from `.env.example` and adds the key.

Other deferred items:
- `gh` CLI is not installed on the dev machine, so the private GitHub repo has
  not been created or pushed. Git history is local-only. Install `gh`, run
  `gh auth login`, then `gh repo create <name> --private --source=. --push`.

Next up: phase 2 — master list builder for Karnataka, then the count sanity
check (a plausible number, not 5 and not 5000) before any per-college scraping
is allowed to start.

## Build order and progress
- [x] 0. Scaffolding — AGENTS.md, context.md, README, git, venv, deps, frontend
- [x] 1. DB schema + models; inert fallback stubs
- [ ] 2. Master list builder, pilot state only (Karnataka)
- [ ] 3. Ollagraph-only pipeline, end-to-end on pilot
- [ ] 4. Evaluate pilot stage-by-stage; wire fallbacks only where needed
- [ ] 5. Excel/CSV export — marketing schema + completeness filter
- [ ] 6. FastAPI endpoints, split marketing vs admin
- [ ] 7. Next.js frontend: marketing + admin views
- [ ] 8. Full re-run on pilot, verify marketing export, then generalize
- [ ] 9. (post-v1) basic auth on the UI

## Project structure
Every file gets a one-line description. Keep in sync with the actual repo.

```
AGENTS.md                          — agent working rules: git, scope, security, attribution
context.md                         — this file: purpose, stack, status, file map
README.md                          — project overview + setup instructions
requirements.txt                   — Python dependencies
.env.example                       — backend env var template (real .env is gitignored)
.gitignore                         — excludes secrets, caches, regenerable seed lists
backend/
├── __init__.py                    — package marker
├── api/__init__.py                — package marker; routes land in phase 6
├── db/
│   ├── session.py                 — sqlite3 connect + get_conn ctx manager; WAL, busy_timeout
│   └── models.py                  — SCHEMA (colleges, scrape_runs) + the marketing
│                                    projection: MARKETING_SELECT, MARKETING_COMPLETENESS_FILTER,
│                                    INTERNAL_ONLY_COLUMNS. The internal/marketing boundary is
│                                    defined HERE, once, not in the API layer.
├── scraper/
│   ├── discovery.py               — FALLBACK, INERT. DuckDuckGo discovery. Raises
│   │                                FallbackNotWired. Holds BLOCKED_DOMAINS (29 aggregators).
│   ├── crawler.py                 — FALLBACK, INERT. BFS crawler. Raises FallbackNotWired.
│   │                                Holds PLACEMENT_PATH_HINTS, SKIP_EXTENSIONS.
│   └── cloudflare_decoder.py      — FALLBACK, IMPLEMENTED BUT UNWIRED. decode_all() /
│                                    decode_cfemail(). Pure transform, no network, so it is
│                                    written and tested now; wiring is a phase-4 decision.
├── tests/
│   ├── test_marketing_projection.py — guards the completeness filter + internal-column leaks
│   └── test_cloudflare_decoder.py   — round-trip, malformed input, document ordering
├── reference/
│   └── state_districts.json       — static district list, 36 states/UTs, 770 districts
└── seed_lists/.gitkeep            — dir marker; cached master-list CSVs (gitignored)
frontend/                          — Next.js 16 App Router scaffold (TS, ESLint, no Tailwind)
├── app/                           — default scaffold pages; real views land in phase 7
├── .env.example                   — NEXT_PUBLIC_API_BASE_URL template
├── AGENTS.md                      — generated BY the Next.js scaffold, not hand-written:
│                                     warns that Next 16 diverges from model training data
│                                     and to consult node_modules/next/dist/docs/. Scoped to
│                                     frontend/; does not override the root AGENTS.md.
├── CLAUDE.md                      — scaffold-generated, just `@AGENTS.md`
└── package.json                   — next, react, react-dom, sonner
```

**Next.js 16 caution:** the scaffolded frontend is Next 16.2.12 with React
19. Per the scaffold's own AGENTS.md, App Router APIs and conventions may
differ from what a model has memorized — check `node_modules/next/dist/docs/`
before writing frontend code in phase 7 rather than relying on recall.

Backend modules and frontend views land in later phases; this section is
updated in the same commit as any file that's added or repurposed.

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

Friction points observed in prior hand-testing — **confirm on the pilot,
do not assume still true**:
- `/v1/scrape/batch` returned page titles only, not usable content. Use
  per-page `/v1/scrape` or `/v1/scrape/smart` for bulk fetching.
- `/v1/crawl` did not follow internal links beyond the seed page — likely
  fallback trigger for the crawl stage.
- Ollagraph's scrape stripped the `data-cfemail` attribute needed to decode
  Cloudflare-obfuscated emails (common on WordPress college sites) — likely
  fallback trigger for extraction.
