# Sort Sherr

A tool that helps college students find off-campus internships at **good** startups, preferring
YC-backed and recently funded ones. The pipeline has four parts:

1. Ingestion (this repo, done): collect startups and their open roles
2. Resume parsing + matching (embeddings + LLM reranking)
3. Personalized email drafting based on company research
4. Outreach tracker (sent / replied / follow-up dates)

**Principle: quality over quantity. This is NOT a spam tool.**

## The owner is a student

They want to understand every part of the code. Keep it simple and readable. Don't over-engineer
or add abstractions "for later". Prefer plain functions, short modules and a comment on *why*.

## Conventions

- **Python 3.11+**, using the `.venv` made from `/opt/homebrew/bin/python3.13`. The system python is 3.10.
- Run things with `.venv/bin/python -m sort_sherr.cli ...` and `.venv/bin/pytest`.
- **Stack:**
  - httpx (async) for HTTP
  - selectolax, using `from selectolax.lexbor import LexborHTMLParser` (the old `selectolax.parser` raises in 1.x)
  - Pydantic v2 for data models
  - plain `sqlite3` for storage
  - Typer for the CLI
  - pytest for tests
  - Playwright (`extractors/browser.py`): fallback when the plain page gives no jobs; renders JS + iframes
  - trafilatura was dropped on purpose: it removes job lists as "boilerplate"
- **One normalized schema:** every extractor returns `list[JobDraft]` (`models.py`).
- **No per-site parsers.** The tiers are, in order:
  1. ATS public API
  2. JSON-LD `JobPosting` (on the page, then its child pages)
  3. LLM
- **Tier 0 = the HN post itself**, stored as a job. Tier-0 jobs and crawled jobs (tiers 1–3) are
  marked inactive separately.
- **All SQL lives in `storage/db.py`.** Keep it portable (no SQLite-only tricks), because we plan
  to move to Postgres + pgvector in Part 2.
- **Company merge rules (`upsert_company`):**
  - match by domain; fall back to the name only when the domain is unknown
  - the same source may update fields
  - a different source only fills empty fields
  - list fields are combined
- **Never drop a company** just because it has no internship posting. Keep it with its hiring signals.
- **Tri-state booleans:** `remote` / `is_internship` are `True`, `False` or `None`. `None` means
  unknown; don't coerce it to False.
- **Failures** are stored per company (`discover_status`, `crawl_status`, `crawl_error`) so that
  `stats` can explain them.

## Ethics (hard rules)

- All HTTP goes through `PoliteClient` (`http.py`), which handles:
  - robots.txt ONLY if `config.RESPECT_ROBOTS` (env `SORT_SHERR_RESPECT_ROBOTS=1`). **Off by default: the owner
    chose maximum data over robots.txt.** Rate limiting, User-Agent and BLOCKED_DOMAINS always stay on.
  - a 1 s per-domain delay
  - bounded concurrency
  - an honest User-Agent
  - retries with backoff
- Never scrape LinkedIn, Wellfound/AngelList, Indeed, Glassdoor, Crunchbase, or any site whose
  terms forbid it. These are in `config.BLOCKED_DOMAINS`.
- Prefer official public APIs and open datasets:
  - ATS APIs
  - yc-oss JSON
  - HN Algolia

## Verified endpoints (Nov 2026)

- Greenhouse: `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
- Lever: `api.lever.co/v0/postings/{slug}?mode=json`. An unknown slug returns `{"ok": false}` with HTTP 200.
- Ashby: `api.ashbyhq.com/posting-api/job-board/{slug}`
- Workable: `apply.workable.com/api/v1/widget/accounts/{slug}`. Returns no descriptions.
- YC: `yc-oss.github.io/api/companies/all.json` (filter on `status == "Active"`) and `hiring.json`
- HN: `hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring`, then `/api/v1/items/{id}`
- Rippling: `ats.rippling.com/api/v2/board/{slug}/jobs`
- Dover (2 calls): `app.dover.com/api/v1/careers-page-slug/{slug}` → id → `/api/v1/careers-page/{id}/jobs`
- Gem: `api.gem.com/job_board/v0/{slug}/job_posts/`
- YC job pages (`ycombinator.com/companies/{slug}/jobs/...`) carry
  JobPosting JSON-LD. They are used as a last-resort careers page.

## Web app

- `sort_sherr/web/`: FastAPI + Jinja2 templates, no JS framework (only a tiny polling script on
  /admin). Start it with `python -m sort_sherr.cli serve`.
- Pages only READ through query functions in `storage/db.py`. Admin actions never write directly:
  they run the normal CLI commands as a subprocess via `web/runner.py` (one task at a time).
- Every form/URL value must be validated (bounded ints, fixed choice lists). Never build shell
  strings; use `create_subprocess_exec` with an argument list.
- Admin is open unless `SORT_SHERR_ADMIN_PASSWORD` is set. The server binds to 127.0.0.1.

## Tests

Tests use saved fixtures only, with **no live network**. Every ATS adapter, the JSON-LD extractor,
the HN parser, the DB rules and Tier 3 have tests. Tier 3 uses a fake `LLMProvider`. When you
change an extractor, refresh or add a fixture taken from a real response (trimmed).

## LLM (Tier 3)

- Sits behind the `LLMProvider` protocol in `extractors/llm.py`.
- **Everything must be free.** The owner has no paid API keys. Tier 3 uses the **Google Gemini
  free tier** (`google-genai` SDK), which needs no credit card. Don't add paid providers.
- Default model: `gemini-3.5-flash-lite`. Override it with `SORT_SHERR_LLM_MODEL`.
- How it's called: `client.aio.models.generate_content` with `response_mime_type="application/json"`
  and `response_schema=LLMJobList`.
- The key comes from `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), usually set in `.env`. `config.py`
  loads `.env` itself. Without a key, Tier 3 is skipped (`tier3_skipped`).
- Free-tier rate limits are handled in `GeminiProvider`:
  - one call at a time
  - `LLM_MIN_INTERVAL` (6 s) between calls
  - retry on 429 after 30/60/90 s
- Free-tier data may be used by Google to improve its products. Only send public pages; never
  send private data such as resumes on the free tier.
- Results are cached in the `llm_cache` table, keyed by sha256 of the URL plus the page text.
