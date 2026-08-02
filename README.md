# College Placement Contact Intelligence Tool

Given an Indian state, this tool builds a master list of every Engineering
and BCA college in that state, discovers each college's official website,
deep-crawls it to find the Training & Placement Officer's (TPO) contact
details, and falls back to the college's general contact info when no
placement-specific contact exists. Results are stored in SQLite and served
through a small Next.js UI where the marketing team can search, filter, and
export clean contact lists to Excel/CSV for outreach.

Ollagraph is the primary engine for every pipeline stage — discovery,
crawling, and extraction all try Ollagraph first. Custom logic (DuckDuckGo
discovery, a BFS site crawler, a Cloudflare email decoder) exists only as a
per-stage fallback, wired in only where Ollagraph demonstrably
underperforms on a real pilot state.

## Status

Phase 0 (scaffolding). No pipeline code yet. See `context.md` for detailed
current status and the build order.

## Setup

### Prerequisites
- Python 3.11+ (developed against 3.14)
- Node.js 18+ (developed against 24)

### Backend

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### Environment

```bash
cp .env.example .env
```

Then edit `.env` and set `OLLAGRAPH_API_KEY` to your real key. `.env` is
gitignored and must never be committed. The pipeline cannot run without
this key — every discovery, crawl, and extraction call goes through
Ollagraph.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

## Usage

Not yet implemented — endpoints and UI land in phases 6 and 7. This section
expands as they're built.

## Cost note

Ollagraph bills per successful call. Master lists are cached per
state+stream to `backend/seed_lists/` and are not regenerated within 30
days unless `force_refresh=true` is passed. Colleges that scraped
successfully are not re-scraped without an explicit refresh flag.

## Project conventions

See `AGENTS.md` for git discipline, scope rules, and security requirements.
See `context.md` for architecture, data schema, and the file map.
