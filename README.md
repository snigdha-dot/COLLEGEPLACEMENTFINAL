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

Backend, pipeline, API, and UI are built and the pipeline is verified working
end-to-end against real Karnataka colleges. 114 tests pass. The full
84-college production run has not been executed yet — see `context.md` for
detailed status, per-endpoint findings, and what remains.

## Setup on a new machine

Start to finish, assuming a fresh clone:

```bash
git clone <repo-url> && cd "COLLEGE PLACEMENT"

# 1. Backend
python -m venv venv
venv\Scripts\activate                      # Windows
pip install -r requirements.txt

# 2. Secrets — .env is NOT in git, you must create it
cp .env.example .env                       # then paste OLLAGRAPH_API_KEY
cp frontend/.env.example frontend/.env.local

# 3. Load the dataset. The SQLite file is gitignored; the data lives in
#    data/colleges_snapshot.csv, which IS committed.
venv/Scripts/python.exe -m backend.snapshot restore

# 4. Frontend
cd frontend && npm install && cd ..
```

Then run the two servers (see **Usage** below) and check
`localhost:3000` shows the expected row count.

### Keeping the dataset in sync

The SQLite file never enters git — it is a binary blob that conflicts on
every concurrent edit. The data travels as a diffable CSV instead:

```bash
python -m backend.snapshot status     # compare DB against the snapshot
python -m backend.snapshot export     # DB  -> data/colleges_snapshot.csv
python -m backend.snapshot restore    # CSV -> DB
```

**Export and commit the snapshot after any scraping run**, otherwise that
work exists only on the machine that did it. Restore merges rather than
replaces: a blank field in an older snapshot will never overwrite a contact
found later, so pulling stale data cannot destroy newer findings.

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

Run the backend and frontend in two terminals:

```bash
# terminal 1 — API on :8000
venv/Scripts/python.exe -m uvicorn backend.main:app --reload

# terminal 2 — UI on :3000
cd frontend && npm run dev
```

Then open:

| URL | What it is |
| --- | --- |
| `localhost:3000` | Marketing view — clean contacts, filters, Excel export |
| `localhost:3000/admin` | QA view — full records, statuses, job controls |
| `localhost:8000/docs` | Interactive API reference |

### Typical first run

1. Open `/admin`, pick a state, click **Build seed list**. This produces the
   master college list and caches it for 30 days.
2. Click **Run scrape (25)**. Colleges are scraped in the background and
   written to SQLite as they finish, so a crash mid-run keeps what completed.
3. Watch the status counts. `Verified` means a placement email *and* phone
   were found; `Needs Follow-up` means partial; `Failed` means nothing.
4. Switch to the marketing view and export. Only rows with **both** an email
   and a phone appear there — that filter is applied server-side and cannot
   be bypassed from the UI.

### Importing an existing dataset

If you already have a spreadsheet of colleges, load it directly — no scraping
required. One file may mix states and streams; each row is read on its own.

```bash
# 1. Always dry-run first: shows the column mapping and counts, writes nothing
venv/Scripts/python.exe -m backend.import_data data.xlsx --dry-run

# 2. Check the mapping looks right, then import
venv/Scripts/python.exe -m backend.import_data data.xlsx

# optional: fill blank cells with a default
venv/Scripts/python.exe -m backend.import_data data.csv --default-state Karnataka --stream BCA
```

Column names are matched loosely, so `College Name`, `college_name`, and
`Name of the College` all work, as do `Email ID` / `Mobile No` / `Course`.
Anything unrecognised is **reported and ignored** rather than guessed at.

What the importer does with messy data:

| Situation | Behaviour |
| --- | --- |
| `N/A`, `-`, `nil`, blank | treated as empty |
| `a@x.in, b@x.in` in one cell | first becomes the contact, rest become backups |
| Same college listed twice | merged; blanks filled, existing values kept |
| Row with no college name | skipped and counted |
| A "phone" that is a year or PIN code | rejected by the same validator the scraper uses |
| Row missing an email **or** a phone | stored, visible in `/admin`, **not** in marketing or the export |

Imported rows are never marked `Verified` and carry confidence `0` — nothing
verified them. They are also safe to scrape over later: the pipeline only
fills fields it finds empty, so it adds what is missing without overwriting
your data.

### Filling in missing phone numbers

Colleges that have an email and a website but no phone are held out of the
marketing view. `fill_phones.py` targets exactly those:

```bash
# always dry-run first — reports findings without writing
venv/Scripts/python.exe -m backend.fill_phones --limit 10 --dry-run

venv/Scripts/python.exe -m backend.fill_phones --limit 100
venv/Scripts/python.exe -m backend.fill_phones --state Karnataka
```

It is much cheaper than the full pipeline because there is no discovery and
no site-wide crawl — the website is already known, so it fetches only
contact-bearing pages (`/contact-us`, `/placement`, homepage), up to 4 per
college.

**It verifies the site before scraping it.** A URL from a spreadsheet can be
dead, parked, or simply wrong, so the fetched page must actually identify as
that college (distinctive word from the name, its acronym, or the domain
name appearing in the text). A site that fails this check is reported
`UNVERIFIED` and skipped rather than scraped — attaching a stranger's phone
number to a row marketing then calls is worse than leaving it blank.

Numbers found are stored as the **fallback** contact, not the placement
contact, since nothing here establishes they belong to the placement cell.

### API endpoints

```
GET   /api/colleges?view=marketing|admin   list, filter, search
GET   /api/colleges/{id}                   full record
PATCH /api/colleges/{id}                   hand-correct a field
GET   /api/colleges/stats                  status counts (QA)
GET   /api/export?format=xlsx|csv          download (always marketing schema)
POST  /api/seed/build                      build the master list
POST  /api/scrape/run                      start a background scrape run
POST  /api/scrape/college/{id}             re-scrape one college
GET   /api/scrape/runs                     run history
```

## Tests

Each test module runs standalone with no test-runner dependency:

```bash
venv/Scripts/python.exe backend/tests/test_marketing_projection.py
venv/Scripts/python.exe backend/tests/test_cloudflare_decoder.py
```

They also work under `pytest` if you install it (`pip install pytest`, dev-only
— deliberately not in `requirements.txt`, which is the runtime dependency set):

```bash
venv/Scripts/python.exe -m pytest backend/tests/ -q
```

The marketing-projection tests guard two rules the brief states as absolutes:
a row reaches marketing only if it has both an email and a phone, and
`status` / `last_scraped` / `confidence_score` never appear in a marketing
payload.

## Cost note

Ollagraph bills per successful call. Master lists are cached per
state+stream to `backend/seed_lists/` and are not regenerated within 30
days unless `force_refresh=true` is passed. Colleges that scraped
successfully are not re-scraped without an explicit refresh flag.

## Project conventions

See `AGENTS.md` for git discipline, scope rules, and security requirements.
See `context.md` for architecture, data schema, and the file map.
