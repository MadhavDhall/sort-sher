# Sort Sherr

A tool that helps college students find off-campus internships at **good** startups, preferring
YC-backed and recently funded ones. The pipeline has four parts:

1. Ingestion (done): collect startups and their open roles
2. Resume parsing + matching (done, `sort_sherr/matching/`): embeddings + LLM reranking
3. Company research + personalized email DRAFTS (done, `sort_sherr/outreach/`): never sends
4. Outreach tracker + reply-rate metric (done, `sort_sherr/tracker/`): event log, follow-ups, stats. Never sends

**Principle: quality over quantity. This is NOT a spam tool.**

It runs locally (SQLite, one user, no login) or hosted for other students (Render free + Neon Postgres +
GitHub Actions, invite-only accounts): see "Hosting" at the end.

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
  - plain `sqlite3` for storage locally; Postgres (psycopg 3, a free Neon database) when `DATABASE_URL` is set
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
- Never scrape LinkedIn, Wellfound/AngelList, Indeed, Glassdoor, Crunchbase, Apollo, RocketReach,
  ZoomInfo, X/Twitter, or any site whose terms forbid it. These are in `config.BLOCKED_DOMAINS`.
  A LinkedIn/X profile URL found on another page may be stored as a link, never fetched.
- **No email-sending code anywhere** (no SMTP, no Gmail send). `test_no_send_code_anywhere` enforces it.
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
- Locally: admin is open unless `SORT_SHERR_ADMIN_PASSWORD` is set, and `serve` binds to 127.0.0.1 only.
  Hosted (`LOGIN_REQUIRED`): every page needs a login and /admin needs an admin account (see Hosting).

## Tests

Tests use saved fixtures only, with **no live network**. Every ATS adapter, the JSON-LD extractor,
the HN parser, the DB rules and Tier 3 have tests. Tier 3 uses a fake `LLMProvider`. When you
change an extractor, refresh or add a fixture taken from a real response (trimmed).
Part 2 tests use a made-up resume (`tests/fixtures/resume_fake.pdf/.docx`, rebuilt by
`make_resume_fixtures.py`) and `companies_fake.json`; never commit a real resume.

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
- Free-tier data may be used by Google to improve its products. Only send public pages, and
  resumes only after local redaction (see Part 2). Never send other private data.
- Results are cached in the `llm_cache` table, keyed by sha256 of the URL plus the page text.
- `GeminiProvider.generate(prompt, schema) -> (parsed, usage)` is the generic call (Part 2 uses it);
  `extract_jobs` is a thin wrapper. Usage = prompt/output tokens from `usage_metadata`.
- Free models can return 503 "high demand" for long stretches. Don't add paid fallbacks; the
  caches make re-running cheap. `SORT_SHERR_LLM_MODEL=gemini-3.5-flash` is the other free model.

## Part 2: matching (`sort_sherr/matching/`)

- Flow: `parse-resume` → `match` (hard filters → embeddings top-K → LLM rerank → final score) →
  `explain` / `eval`. `run.py` wires one run together.
- **Company-level matching:** most companies have no posting, so match the company and use jobs as
  extra signal. Filters drop only on KNOWN mismatches; unknown is kept and counted.
- **Resume privacy (hard rule):** `resume.redact()` removes name, email, phone, URLs and 7+ digit IDs
  locally before any LLM call; PII is stored only in `candidate_profiles.pii_json`. Templates put
  the name anywhere, so `find_name()` only guesses and the CLI always has the student confirm the
  name before sending (`--name` / `--yes` skip the question). With no name, refuse rather than send.
  Never log/print resume text.
- Facts the student knows better than the LLM (graduation year) are asked after parsing
  (`--grad-year`) and saved over the LLM's value; re-running on a cached file costs no LLM call.
  `uploads/` and `labels/` are gitignored.
- Preferences (roles, remote, locations, paid, window, free-text **interests**) come from CLI flags,
  never the resume. Interests add up to `TOP_K_INTERESTS` extra companies (`found_by`).
- Embeddings: `Embedder` protocol; default local `BAAI/bge-small-en-v1.5` (free, private). Cached
  in `embeddings` by sha256(text). Plain numpy cosine, no vector DB.
- Rerank: rubric of 5 sub-scores × 0–20 (stack, domain, interest, seniority, hiring signal), plus
  `why_fit`, `why_this_role`, evidence pairs (resume item ↔ company fact) and red flags. Ungrounded
  evidence and invalid `best_job_id` are removed in `clean_result`. Cached in `rerank_cache`.
- **All tunables live in `match_config.py`** (top-K, batch size, weights, boosts, token prices).
- Every run is kept (`match_runs` + every pool company in `matches`) so runs can be compared and
  `eval` can compare retrieval-only vs reranked orderings of the same run.
- Tests use `FakeEmbedder` (bag of words, in `conftest.py`) and fake LLMs; no model downloads.
- Shortlist size (`--top`, saved as `Preferences.shortlist`) drives the pool: `match_config.pool_size()`
  = 1.5 × shortlist, at least `TOP_K_RESUME`, at most `MAX_POOL`. The rerank also returns
  `pitch_role` (what to ask for in a cold email), since most companies post no fitting role.
- Website: `web/match_pages.py` (`/match/...`) = upload → confirm name + grad year → profile with
  suggested roles (`matching/roles.py`, no LLM) → match form → results + CSV. Same rules as admin:
  read-only queries, writes go through `runner` running the CLI, all form values validated, free
  text passed as `--opt=value` (never parsed as a flag), uploads saved under a hash name.
  Locally the Match pages are open (owner's choice: no password; 127.0.0.1 only). Hosted, every
  /match page requires a login and `auth.owner_guard` restricts it to the student's own data.
  Match has its own `/match/task.json` + `/match/stop` that only see/stop Match tasks (hosted: only
  the student's OWN task), never admin crawls.

## Part 3: research + drafts (`sort_sherr/outreach/`)

- Flow: `draft` (`run.py`) → for each top match of the latest reranked run: `research.py` → `contacts.py`
  → `draft.py`. Then `review` (CLI) → pick variant → `$EDITOR` → approve → `drafts/<company>.txt`.
- **Research:** reads the homepage, up to 5 own-site pages (`USEFUL_PAGES`), the careers page, the YC page and HN.
  - The YC page's `data-page` JSON becomes text pages: company, founders + links, jobs, Launch YC posts, news headlines.
  - HN: Algolia stories, kept by the strict `relevant_story` filter. The company's own HN hiring post comes from tier-0 jobs.
  - One LLM call → `CompanyBrief`. `clean_brief` drops facts whose `source_url` isn't a page we read,
    and people not named on their source page.
  - Saved in `company_briefs` with `pages_json`, so contacts can be re-read without fetching.
    Refreshed after `BRIEF_MAX_AGE_DAYS`.
- **Contacts:** rule-based, no LLM.
  - Sources: emails/tel/social links on first-party pages (`FIRST_PARTY_KINDS`); third-party pages research
    already read (`hn`/`news`: company-domain emails + person-tied social links only); the GitHub users API
    (only for profiles a first-party page linked); founders' own sites from GitHub's `blog` field
    (`MAX_FOUNDER_SITES`, first-party/medium); and optional Hunter (`EmailFinder`, `HUNTER_API_KEY`).
    X bios are never fetched (blocked); GitHub's `twitter_username` gives the handle.
  - Email rule (owner's choice): on the company domain → kept. On another domain → kept only if a first-party
    page published it (startups use gmail), with medium confidence and a note. Off-domain from GitHub/Hunter/founder sites/third-party pages → dropped (personal).
  - Phones only from the company's own site.
  - Pattern guesses are `is_guess=1` and never the default.
  - MX via dnspython only, never SMTP.
  - Ranking (`level`): first-party email > API-verified > third-party email > LinkedIn/X > apply URL.
    Unknown role inboxes (no person, not hello@/contact@/info@, e.g. research@), bad inboxes (sales/support...)
    and no-MX domains drop below DMs (owner's choice). Any local part containing "founder" counts as a founders inbox.
  - `forget-contact` also adds the value to `forgotten_contacts`, so it is never saved again.
- **Drafts:**
  - Prompt rules live in `outreach/prompts/*.md` (+ `banned_phrases.txt`), never inline.
  - Facts are cited as F-ids (`numbered_facts`), resume items as S-ids (`student_items`, which have no name or links).
  - **Privacy:** the LLM only sees `[NAME]`/`[GITHUB]`/`[LINKEDIN]`/`[PORTFOLIO]`. They are filled locally
    from `pii_json`/`profile.links` after generation.
  - Checks: `rule_problems` (ids exist, ≥1 fact, every number appears in a source) + the LLM `grounding_check.md`
    + hard lint. On failure: one rewrite with the problems as feedback, then `needs_review`.
  - Similarity to other companies' drafts is only flagged.
  - The ask uses the posted role only if it's an internship (`build_ask`), else Part 2's `pitch_role`.
  - Signature links: `draft --github/--linkedin/--portfolio`, saved in `pii_json`. Invented link placeholders
    (`[GITHUB_LINK]`) are normalized; ones without a value are removed in `fill_placeholders`.
  - Lint also flags generic subjects and one-block bodies (both are hard problems, so they trigger a rewrite).
- **Guardrails:**
  - `DAILY_DRAFT_CAP` (10) per day per candidate: new companies + Part 4 follow-up drafts
  - companies already written to are always skipped (`contacted_company_ids` reads Part 4's `outreach`)
  - companies already drafted are skipped unless `--redo`
- All tunables are in `outreach_config.py`. Token usage of every Part 3 command → `outreach_runs`.
- Eval: `eval-drafts` = LLM judge (`judge.md`, 5 criteria 1–5) → `labels/draft_ratings.csv` with a `my_rating`
  column (kept on re-runs); `--report` = mean abs diff, within-1, Pearson.
- Tests: `FakeClient` (conftest, serves saved pages by URL), fake LLMs, stubbed MX. A `no_real_browser`
  autouse fixture keeps Playwright off. Fixtures are trimmed real AfterQuery/YC/HN/GitHub responses; the Hunter
  fixture follows the docs' shape (no key was available).
- `drafts/` is gitignored. Contacts and drafts are personal data and stay in the local DB.
- Website: `web/draft_pages.py` (`/match/candidates/{cid}/drafts...`), same access rules as /match.
  - Writes go through the CLI. The long `draft` runs through `runner` as task "Draft emails" (a `MATCH_TASKS`
    entry, so /match/task can follow it). Quick ones (`approve-draft`, `mark-contacted`, `forget-contact`) use
    `runner.run_now`, which is exec with an argument list and a timeout.
  - Edited text is passed as `--subject=` / `--body=`. Form values are checked: top from `TOP_CHOICES`,
    window by regex, setup in `SETUPS`, links https only, and the draft/contact id must belong to the page's company.
  - The .txt download is built from the DB with `draft.export_text` (the same text the CLI writes to drafts/).

## Part 4: outreach tracker (`sort_sherr/tracker/`)

- **Never sends.** The student sends from their own inbox, then logs it (`sent`, `reply`, `status`, or the website).
  Gmail integration was skipped on purpose (owner's choice); logging is manual for every channel.
- **Storage (owner's rule: proper relational tables).** `outreach` (one per candidate+company; current `status`,
  `channel`, `reply_type`; FKs to the draft and the contact, never copies) + `outreach_events` (the log: kind, at,
  note, source manual/auto/migrated, followup_number, scheduled_for). CHECK constraints on every enum,
  `PRAGMA foreign_keys = ON` in `connect()`. `email_drafts` gained `outreach_id` + `followup_number` (0 = first email).
  - `record_event` writes the event AND updates outreach in one transaction.
  - Events are read in insertion order (`ORDER BY id`); `at` is only for timings (a back-dated send stays after "drafted").
  - `save_contacts` keeps contacts an outreach references (research refreshes must not break the link).
  - Part 3's `contacted` rows are migrated on connect (idempotent); the old table is kept but no longer written.
  - Part 3 views stay first-email only: `candidate_drafts`, `delete_drafts`, `companies_drafted_since` filter
    `followup_number = 0`; `approve_draft` only un-chooses variants of the same message.
- **Lifecycle** (`lifecycle.py`, `NEXT`): drafted → sent → replied → interview → offer/rejected; sent → bounced/ghosted;
  any → withdrawn; ghosted → replied (late reply). A reply must be logged before interview/rejected/offer.
  `answered` and `followup_sent` are events, not statuses. Approving a first draft creates the `drafted` outreach;
  `sent <draft_id>` on a follow-up draft logs `followup_sent`. Dates from users are LOCAL time (`parse_when`).
- **Follow-ups** (`followups.py`, numbers in `tracker_config.py`): business days Mon–Fri, no holidays. FU1 = sent + 5,
  FU2 = ACTUAL FU1 send + 7, ghosted = actual FU2 send + 7. Only `sent` outreach gets follow-ups (any reply/bounce/final
  status stops them). `due` sorts by priority (interviews → replies → follow-ups → ghosted → approved-not-sent) and
  auto-marks ghosted (source `auto`); the website only shows it (pages never write).
- **Follow-up drafts** (`followup_draft.py`, prompt `followup_email.md`): one variant, 25–60 words, subject
  `Re: <first subject>`, ONE new thing: `--value` (redacted with `resume.redact`, added as a new S item) or a brief
  fact the first email didn't cite (recent_updates first). Reuses Part 3's `rule_problems`, `grounding_problems`,
  `fill_placeholders`; `lint(..., followup=True)` skips subject checks and bans bumps (`followup_banned_phrases.txt`).
  One rewrite, then `needs_review`. Counts toward `DAILY_DRAFT_CAP`. Approved via `approve-draft` / the CLI loop;
  exported to `drafts/<company>-followup-<n>.txt`. The website runs `followup --no-review` as task "Draft follow-up".
- **Metrics** (`metrics.py`): reply rate = replied / (sent − bounced); positive, interview rate, median days to first
  reply. Breakdowns: variant, recipient role, which message got the reply, contact type/source, score bucket, YC batch
  year, team size, weekday, channel. Every group shows n; n < `SMALL_N` (10) is flagged and `verdict()` names a leader
  only when every group has n ≥ 10. Feedback loop: `match` output / shortlist page show outreach badges + reply rates by
  team size/batch/recipient. Information only; it never changes ranking.
- **Outputs:** `outreach-stats` (terminal), `report --out` (one self-contained HTML, Jinja2 + inline SVG, no JS),
  `export --out` (CSV without contact values or notes). Nothing is written unless `--out` is given; `reports/` and
  `exports/` are gitignored. No reply text ever goes to an LLM.
- **`forget`:** `--company` deletes outreach (+events, follow-up drafts by cascade), drafts, contacts (to
  `forgotten_contacts`), `contacted` rows and `drafts/<slug>*.txt`; public company data stays. `--contact` deletes one
  value everywhere (outreach keeps its history with `contact_id` NULL).
- **Website** (`web/outreach_pages.py`, same access rules as /match): `/match/candidates/{cid}/outreach` (Today + table),
  `/outreach/stats` (+ `report.html`, `export.csv` built on request), `/outreach/{oid}` (timeline, reply/status forms
  with only allowed moves, follow-up drafts). Drafts page: "I sent it" posts `/drafts/{company}/sent`. Same rules as
  admin: read-only queries, writes via `run_now`/runner with argument lists, ids must belong to the candidate,
  enums from fixed lists, dates by regex, free text as `--opt=value` (≤300 chars).
- Tests: `outreach_db` fixture (conftest), `test_tracker.py` (model, migration, FKs, CLI, forget), `test_followups.py`
  (dates at 12:00 UTC so any time zone works), `test_metrics.py` (rates, small n, report/CSV, follow-up drafts with a
  fake LLM, cap), `test_outreach_web.py` (pages + exact CLI args).

## Hosting (Render free + Neon + GitHub Actions; $0/month)

- **Owner's budget rule:** $50 of Render credits, never more, maybe shared with 1-2 other projects. So: Render
  FREE web service (sleeps after 15 min, 512 MB, temporary disk), Neon FREE Postgres (one project per app, never
  expires), GitHub Actions for heavy jobs. Never add a Render disk, Render Postgres or any paid service.
- **Database** (`storage/db.py`): one set of SQL for both backends. `connect(path)` returns sqlite3 for a file /
  `:memory:`, a `PgConnection` for a `postgresql://` URL (`config.DB_PATH = DATABASE_URL or SQLITE_PATH`).
  Write SQL that both accept: `?` placeholders (turned into `%s`; a literal `%` is escaped when there are
  params), `RETURNING id` (never `lastrowid`), `ON CONFLICT ... DO UPDATE/NOTHING` (never `INSERT OR REPLACE`),
  `NULLS LAST` on any DESC sort of a nullable column, `lower(x) LIKE` for search, `CASE WHEN` instead of
  boolean sums. Python bools are sent as 1/0 (INTEGER columns). PgConnection is autocommit; `with conn:`
  is one transaction. `pg_schema()` adapts the DDL (identity ids, BYTEA, DOUBLE PRECISION).
  - `copy-to-postgres` copies the local SQLite file into Neon once (ids kept, re-runnable, sequences reset;
    `TABLES` lists parents before children; drafts <-> outreach handled in two passes).
  - Tests: SQLite by default. `SORT_SHERR_TEST_PG=postgresql://... pytest` runs the WHOLE suite on Postgres
    (conftest maps each test database to a throwaway schema). Run it after any SQL change.
- **Accounts** (`web/auth.py`), active when `config.LOGIN_REQUIRED` (Render sets `RENDER`; or `SORT_SHERR_LOGIN=1`):
  - Invite-only (no email-sending code, so no email sign-up or reset): the admin makes one-time `/join/<code>`
    links on /admin (or `invite` CLI); a link with `for_user_id` resets that user's password. Only sha256 of codes
    is stored. Passwords: stdlib scrypt. Login: Starlette `SessionMiddleware` signed cookie (uid, name, admin),
    https-only on Render, SameSite=lax. 5 wrong passwords lock a username for 5 min (in memory).
  - `login_gate` middleware: everything except /login, /join/, /static/, /healthz needs a session.
  - `owner_guard` is a dependency on the Match/Draft/Outreach ROUTERS (app.py): the cid/candidate_id/run_id
    in the URL must belong to the session's user, else 404. Don't bypass it with candidate ids from forms.
  - `candidate_profiles.user_id`; resume cache key = sha256("user<id>:" + file) so students never share one.
    Uploads are deleted after parsing (`parse-resume --delete-file`); `/match/resume/<file_id>` finds the
    candidate by hash prefix + user. Runner tasks have an `owner`; students see only their own task log.
  - Admin-only on the web when hosted: /admin, forgetting a (shared) contact. Students can delete a resume and
    everything made from it (`forget --candidate`). Per-student limits: `MAX_RESUMES_PER_USER`,
    `MAX_MATCH_RUNS_PER_DAY` (shared free Gemini key), plus the existing draft cap.
  - Exception to "web writes go through the CLI": account writes (join, password, invites) happen in the web
    process, because a password must never be a process argument. `create-user` asks for it on stdin.
- **Render** (`render.yaml`, `.python-version` 3.13): `pip install -e ".[deploy]"` (no torch, no Playwright),
  model downloaded at build (`embed-companies --download-only` into `models/`), uvicorn with `--proxy-headers`.
  `/admin` hides crawl buttons when hosted and links to the GitHub workflow (`SORT_SHERR_REFRESH_URL`).
- **Embeddings when hosted:** `SORT_SHERR_EMBEDDER=fastembed` = the same bge-small model on ONNX (vectors match
  torch's, cosine 1.0), cached under the name `fastembed:<model>`. Measured: web ~80 MB + a match subprocess
  ~340 MB peak. `embed_cached` works in chunks of 256 (saved as it goes), fastembed batch 8: embedding all
  4.6k companies peaks ~400 MB.
- **GitHub Actions** (`.github/workflows/refresh.yml`): weekly + "Run workflow": seed yc + hn, discover/crawl
  `--limit`, `embed-companies`, `stats`. Secrets `DATABASE_URL` (required), `GEMINI_API_KEY` (optional).
  `timeout-minutes: 120`, one run at a time.

