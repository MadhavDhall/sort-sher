# Sort Sherr

Sort Sherr helps college students find **off-campus internships at good startups**, with
YC-backed and recently funded companies first.

The full project has four parts. **This repo is Part 1.**

| # | Part | Status |
|---|---|---|
| 1 | **Ingestion**: collect startups and their open roles | ✅ Built (this repo) |
| 2 | Resume parsing + matching (embeddings + LLM reranking) | ⏳ Not started |
| 3 | Personalized email drafting based on company research | ⏳ Not started |
| 4 | Outreach tracker (sent / replied / follow-up dates) | ⏳ Not started |

> Quality over quantity. This is **not** a spam tool.

📖 **Want to understand the code?** Read [HOW_IT_WORKS.md](HOW_IT_WORKS.md). It walks through every
file with a real example.

---

## What Part 1 does, in one picture

```
 1. SEED                 2. DISCOVER                     3. CRAWL                         4. STATS
 ───────                 ───────────                     ─────                            ─────
 YC company list   ──►   visit each homepage       ──►   get the open jobs:         ──►   how many worked,
 HN "Who is hiring"      find the Careers page            Tier 1  ATS API                  per tier / ATS,
                         spot the ATS (Ashby, ...)        Tier 2  JSON-LD on page          and why the rest
                                                          Tier 3  LLM reads the page       failed
        │                        │                                │
        └────────────────────────┴──────── all saved in SQLite ───┘   (data/sort_sherr.db)
```

## What's built

| Feature | Status | Notes |
|---|---|---|
| Load YC companies (only "Active") | ✅ Works | ~4,400 companies, from the open yc-oss dataset |
| Load HN "Who is hiring?" posts | ✅ Works | ~210 companies per month; parses role, location, remote, email |
| Quality score (0–100) for each company | ✅ Works | Ranks good, recently funded YC startups first |
| Find careers page + detect ATS | ✅ Works | Greenhouse, Lever, Ashby, Workable, Rippling, Dover, Gem |
| Tier 1: ATS public APIs | ✅ Works | The most reliable source |
| Tier 2: JSON-LD on careers/job pages | ✅ Works | Also follows links to individual job pages |
| Tier 3: LLM reads the page | ⚠️ Built + tested, **not yet run live** | Uses Google Gemini's **free** tier; needs a free `GEMINI_API_KEY` ([how to get one](#optional-turn-on-tier-3-llm-with-a-free-gemini-key)) |
| JavaScript-loaded pages (Playwright browser) | ✅ Works | If the plain page gives no jobs, it is opened in a real headless Chrome and tried again |
| YC jobs page fallback | ✅ Works | Last resort for YC companies whose own site gave nothing |
| Dedupe jobs + mark removed jobs inactive | ✅ Works | Tested |
| Politeness: rate limits, honest User-Agent, blocklist | ✅ Works | robots.txt is **off by default** (your choice, for more data); LinkedIn, Wellfound etc. are always blocked |
| Web app: jobs, companies, admin panel | ✅ Works | `python -m sort_sherr.cli serve` → http://127.0.0.1:8000 |
| Offline tests | ✅ 101 passing | Use saved real responses, no internet needed |

### Results from the first real run (24 Nov 2026)

These are for the top 20 YC companies by quality score:

| How jobs were found | Companies | Jobs |
|---|---|---|
| Tier 1 – ATS API | 13 | 236 (4 internships) |
| Tier 2 – JSON-LD | 3 | 24 |
| Need Tier 3 (no API key yet) | 4 | – |

For 20 companies from the HN thread, 12 resolved (9 by Tier 1, 3 by Tier 2), 6 need Tier 3, and 2
had no website or no careers page.

---

## Words you'll see

| Word | Meaning |
|---|---|
| **YC** | Y Combinator, the startup accelerator. Its companies are our main target. |
| **Batch** | When a company did YC, e.g. "Winter 2025". A recent batch usually means recent funding. |
| **HN** | Hacker News, a tech forum. Every month it has an "Ask HN: Who is hiring?" thread. |
| **ATS** | Applicant Tracking System: the software companies use to post jobs (Greenhouse, Lever, Ashby, Workable, Rippling, Dover, Gem). |
| **Slug** | A company's ID on an ATS. In `jobs.ashbyhq.com/mem0`, the slug is `mem0`. |
| **JSON-LD** | Hidden machine-readable data inside a web page. Job pages use it so Google can index them. |
| **Tier** | How a job was found: 0 = HN post, 1 = ATS API, 2 = JSON-LD, 3 = LLM. |
| **quality_score** | A 0–100 rating of how good a company is to target (see below). |

---

## Setup

You need Python 3.11+. This Mac's default Python is 3.10, so use Homebrew's 3.13:

```bash
cd "Sort sherr"
/opt/homebrew/bin/python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Optional: turn on Tier 3 (LLM) with a FREE Gemini key

Tier 3 uses **Google Gemini's free tier**. It's free, and Google doesn't ask for a credit card.
Everything else works without it.

1. Go to **https://aistudio.google.com/apikey** and sign in with any Google account
   (a personal Gmail works best; some college/work accounts have AI Studio turned off).
2. Accept the terms if asked, then click **"Create API key"**. If it asks for a project, let it
   create a new one. **Don't** click "Set up billing"; the free tier needs no billing.
3. Copy the key (it starts with `AIza...`).
4. Put it in a `.env` file in the project folder:

   ```bash
   cp .env.example .env
   # open .env and set:  GEMINI_API_KEY=AIza...your key...
   ```

   The app reads `.env` automatically. `.env` is in `.gitignore`, so the key never gets
   committed. Never paste it in code or share it.
5. Check that it's on: `python -m sort_sherr.cli crawl --limit 5`. The first line should say
   `tier 3 LLM: on, gemini-3.5-flash-lite`.

**Free-tier limits.** You get a handful of requests per minute and a daily cap per model. To see
your exact numbers, open AI Studio → your project → **Rate limits**. To stay under them:

- The app sends one LLM call at a time, 6 seconds apart.
- If Google answers "429 too many requests", it waits 30/60/90 s and retries.
- Results are cached, so a page is never sent twice unless it changed.

If you hit the daily cap, run `crawl` again the next day. Already-cached pages cost nothing.

> ⚠️ On the free tier, Google may use what you send to improve its products. We only send public
> careers pages, so that's fine here. **Don't** use the free tier for private data like resumes
> (relevant for Part 2).

### JavaScript-heavy careers pages (recommended)

Some careers pages load their job list with JavaScript. To read them, install the browser once (~100 MB):

```bash
pip install -e ".[browser]" && playwright install chromium
```

## How to run

Run the four steps in order:

```bash
# 1. SEED: load companies
python -m sort_sherr.cli seed                  # all Active YC companies (~4,400)
python -m sort_sherr.cli seed --limit 20       # or only the 20 best by quality_score
python -m sort_sherr.cli seed --source hn      # latest HN "Who is hiring?" thread

# 2. DISCOVER: find careers pages + ATS (best companies first)
python -m sort_sherr.cli discover --limit 50
python -m sort_sherr.cli discover --source hn --limit 20

# 3. CRAWL: get the jobs
python -m sort_sherr.cli crawl                 # companies with quality_score >= 40
python -m sort_sherr.cli crawl --source hn --min-score 0

# 4. STATS: see what worked and what failed
python -m sort_sherr.cli stats
```

Run the tests (offline):

```bash
pytest
```

Every command has `--help`, for example `python -m sort_sherr.cli crawl --help`.

## The web app (jobs, companies, admin)

```bash
python -m sort_sherr.cli serve          # then open http://127.0.0.1:8000
```

| Page | What you can do |
|---|---|
| **Jobs** (`/`) | Search every open role, filter by **internships only**, remote, YC/HN, how it was found, minimum company score; sort by best company or newest; open the description; click **Apply** |
| **Companies** (`/companies`) | Search companies, filter by "has open roles", "has internships", hiring, job board (ATS), score; sort by score / most roles / most internships |
| **Company page** (`/companies/{id}`) | All details, **why it got its score** (point by point), crawl status, open roles, and closed roles (history) |
| **Admin** (`/admin`) | Live numbers (per tier, per ATS, failures) and buttons to **get new data**: Full refresh, Seed, Discover, Crawl (incl. "retry only failed ones"). The command's output shows **live** on the page, and you can Stop it |

**Themes.** The site is always light. **🦁 Lion** (default) is the clean original look with lion-gold
only as highlights (buttons, active tab, links, a thin gold line on top). **Classic** is the
original blue. Switch with the button at the top right; your browser remembers the choice.

**Admin password.** The admin page can start crawls. On your own laptop it's open. Before letting
anyone else reach the site, set `SORT_SHERR_ADMIN_PASSWORD=...` in `.env`: the browser will then
ask for it (any username). The server only listens on `127.0.0.1` (this computer) by default.

## Looking at the data

Everything is in one SQLite file, `data/sort_sherr.db`, with three tables:

| Table | One row per | Key columns |
|---|---|---|
| `companies` | startup | name, website, yc_batch, team_size, is_hiring, quality_score, careers_url, ats_type, crawl_status |
| `jobs` | job posting | title, location, remote, is_internship, apply_url, source_tier, first_seen_at, last_seen_at, is_active |
| `llm_cache` | page the LLM has read | content_hash, jobs_json |

To see active internships, best companies first:

```bash
sqlite3 -header -column data/sort_sherr.db "
  SELECT c.name, c.yc_batch, j.title, j.location, j.apply_url
  FROM jobs j JOIN companies c ON c.id = j.company_id
  WHERE j.is_active = 1 AND j.is_internship = 1
  ORDER BY c.quality_score DESC;"
```

---

## How companies are ranked (quality_score)

There's no free funding dataset, and Crunchbase forbids scraping, so we use YC signals instead:

| Signal | Points | Why |
|---|---|---|
| YC batch in the last 2 years (fading to 0 at 6 years) | up to 40 | Recent batch ≈ recent seed money |
| Stage = Growth | 15 | Past the earliest, riskiest stage |
| YC "top company" | 15 | YC's own quality flag |
| Marked as hiring | 15 | They want people now |
| Team of 5–200 | 15 | Big enough to mentor, small enough to need interns |
| Also posted on HN "Who is hiring" | 10 | Actively looking this month |
| Only on HN (not YC) | flat 30 | Kept, but ranked lower |

By default, `crawl` only processes companies scoring **40 or more**.

## Rules the code follows

- **Never drop a company** just because it has no internship posting. Many startups take interns
  without posting the role.
- **No duplicates.** A job is identified by its apply URL, or by title + location when there's no URL.
- **Jobs that disappear** are marked `is_active = 0`, not deleted, so we keep the history.
- **"Unknown" stays unknown.** `remote` and `is_internship` can be true, false, or empty (unknown).
- **Be polite to websites:**
  - robots.txt is **not** enforced by default (set `SORT_SHERR_RESPECT_ROBOTS=1` in `.env` to obey it)
  - send at most 1 request per second per site
  - use an honest User-Agent
  - retry slowly on errors
  - never touch LinkedIn, Wellfound, Indeed, Glassdoor or Crunchbase

## Project layout

```
Sort sherr/
├── README.md              ← you are here
├── HOW_IT_WORKS.md        ← detailed walkthrough of the code
├── CLAUDE.md              ← project rules for the AI coding assistant
├── pyproject.toml         ← dependencies
├── sort_sherr/
│   ├── cli.py             ← commands: seed, discover, crawl, stats, serve
│   ├── web/               ← the website: app.py (routes), runner.py (admin tasks), templates/, static/
│   ├── config.py          ← settings (User-Agent, limits, DB path, LLM model)
│   ├── models.py          ← data shapes: Company, JobDraft, Job
│   ├── scoring.py         ← quality_score
│   ├── http.py            ← polite web client (rate limit, retries, blocklist, optional robots.txt)
│   ├── storage/db.py      ← all database code
│   ├── sources/yc.py      ← loads YC companies
│   ├── sources/hn.py      ← parses HN "Who is hiring?" posts
│   ├── discovery/careers.py ← homepage → careers page
│   ├── discovery/ats.py   ← detects which ATS + slug
│   └── extractors/
│       ├── pipeline.py    ← tries Tier 1 → 2 → 3
│       ├── ats/           ← Tier 1: greenhouse, lever, ashby, workable, rippling, dover, gem
│       ├── jsonld.py      ← Tier 2
│       ├── llm.py         ← Tier 3
│       ├── browser.py     ← headless Chrome for JavaScript pages
│       └── text.py        ← small helpers (HTML → text, dates)
└── tests/                 ← one test file per part, real saved responses in tests/fixtures/
```

## Known limits

- **HN parsing is a best guess.** Posts have no strict format, and about 10% are skipped. Sometimes
  the wrong part ends up as "location".
- **Careers-link detection is a best guess too.** It sometimes picks the wrong page
  (for example `/inbound`).
- **JavaScript pages need Playwright installed.** Without it, those pages are read as the empty shell the server sends.
- **Tier 3 can be wrong.** An LLM saying "no jobs" is less trustworthy than an empty ATS board.
- **SQLite for now.** We'll move to Postgres + pgvector in Part 2, for resume matching.
