# How Sort Sherr works (Part 1: ingestion)

This document explains **every part of the code**: what each file does, why it was built that
way, and how the parts connect. Read it top to bottom once, with the code open beside it.

**Contents**

1. [The big idea](#1-the-big-idea)
2. [Follow one company through the whole system](#2-follow-one-company-through-the-whole-system)
3. [The files, one by one](#3-the-files-one-by-one)
4. [Async, explained simply](#4-async-explained-simply)
5. [The life of a job in the database](#5-the-life-of-a-job-in-the-database)
6. [Tests](#6-tests)
7. [Design decisions and why](#7-design-decisions-and-why)
8. [How to extend it](#8-how-to-extend-it)
9. [Try it yourself](#9-try-it-yourself)

---

## 1. The big idea

We want a list of good startups and their open roles. Startups put their jobs in many different
places, so we can't write one scraper per website (thousands of sites, and they change all the
time). Instead, we **try a few general methods, from most reliable to least**, and stop at the
first one that works:

| Tier | Method | Reliability | Cost |
|---|---|---|---|
| 1 | Ask the company's **ATS** (Greenhouse, Lever, Ashby, Workable, Rippling, Dover, Gem) through its public JSON API | ⭐⭐⭐ Exact data | Free |
| 2 | Read the **JSON-LD** hidden in the careers page (data meant for Google) | ⭐⭐ Structured | Free |
| 3 | Give the page text to an **LLM** and ask it to list the jobs | ⭐ Can make mistakes | Costs money, so results are cached |

All three tiers return jobs in **the same shape** (`JobDraft`). The rest of the program never needs
to know which tier found a job; it is just recorded in the `source_tier` column.

The program runs in four steps, and each step is one CLI command:

```
seed  →  discover  →  crawl  →  stats
```

| Step | Question it answers | Writes to the DB |
|---|---|---|
| `seed` | Which companies exist? | `companies` rows (name, website, YC info, quality_score) |
| `discover` | Where are their jobs? | `careers_url`, `ats_type`, `ats_slug`, `discover_status` |
| `crawl` | What jobs are open right now? | `jobs` rows + `crawl_status`, `crawl_error` |
| `stats` | How well did it work? | nothing (read-only) |

Steps are separate so you can re-run one without the others. For example, run `crawl` every day,
but `discover` only once a week.

---

## 2. Follow one company through the whole system

Let's follow **Mem0**, a real YC company, from start to finish.

### Step 1: `seed`

`cli.py → seed()` calls `sources/yc.py → fetch_yc_companies()`. This downloads one big JSON file
from `https://yc-oss.github.io/api/companies/all.json` (about 6,400 companies). Mem0's entry
looks roughly like this:

```json
{ "name": "Mem0", "slug": "mem0", "website": "https://mem0.ai", "batch": "Summer 2024",
  "stage": "Growth", "team_size": 10, "isHiring": true, "status": "Active", ... }
```

Next, `to_company()` converts it into our `Company` model (from `models.py`):

```python
Company(name="Mem0", website="https://mem0.ai", domain="mem0.ai", sources=["yc"],
        yc_slug="mem0", yc_batch="Summer 2024", team_size=10, is_hiring=True, stage="Growth", ...)
```

Companies that are not `"Active"` (Inactive, Acquired, Public) are skipped here.

Then `db.upsert_company()` saves it. Before saving, it calls `scoring.quality_score()`:

| Signal | Mem0 | Points |
|---|---|---|
| Batch Summer 2024, about 2.5 years ago (40 points fading to 0 at 6 years) | | ~35 |
| Stage Growth | ✔ | 15 |
| Top company | ✘ | 0 |
| Hiring | ✔ | 15 |
| Team of 10 (between 5 and 200) | ✔ | 15 |
| **Total** | | **~80** |

### Step 2: `discover`

`cli.py → discover()` loads companies that haven't been discovered yet (best score first). For
each one it calls `discovery/careers.py → discover()`:

1. **Fetch the homepage** `https://mem0.ai` through our polite HTTP client.
2. **Look for an ATS link on the homepage.** `discovery/ats.py → detect_ats()` searches the HTML
   for patterns like `jobs.ashbyhq.com/...`. Mem0's homepage has none.
3. **Find a careers link.** `find_careers_link()` checks every `<a href>` on the page. It looks
   for a URL like `/careers` or `/jobs`, or link text like "Careers" or "Join us". It finds
   `./careers`.
4. **Fetch `https://mem0.ai/careers`** and run `detect_ats()` again. This time it finds links
   like `https://jobs.ashbyhq.com/mem0/5fdf00fb-...`.
   → ATS = **ashby**, slug = **mem0**.

The result is saved: `discover_status="ats"`, `ats_type="ashby"`, `ats_slug="mem0"`.

### Step 3: `crawl`

`cli.py → crawl()` calls `extractors/pipeline.py → extract_jobs()` for Mem0:

- **Tier 1:** Mem0 has an ATS, so we call
  `extractors/ats/ashby.py → api_url("mem0")` = `https://api.ashbyhq.com/posting-api/job-board/mem0`.
  The API returns JSON, and `ashby.parse()` turns each job into a `JobDraft`:

  ```python
  JobDraft(title="SEO/ AEO Lead", team="Growth", location="India", remote=True,
           is_internship=False, employment_type="FullTime",
           apply_url="https://jobs.ashbyhq.com/mem0/c5d3ced3-...", posted_at=...)
  ```

- That worked, so **we stop**. Tiers 2 and 3 never run for Mem0.

Back in `cli.py`, `db.save_jobs()` stores the jobs (see [section 5](#5-the-life-of-a-job-in-the-database)).
The company gets `crawl_status="ok"`.

### What if Mem0 had no ATS?

Take Tasklet, a real example. Discover finds `https://tasklet.ai/careers` but no ATS. So in `crawl`:

- **Tier 1** is skipped (there's no ATS).
- **Tier 2:** `jsonld.extract_jobs()` looks for JSON-LD on `/careers` and finds none. It then
  follows links under that page, like `/careers/founding-engineer`. Those individual job pages
  **do** have JSON-LD, which gives 5 jobs. Done.
- If Tier 2 had also failed, **Tier 3** would turn the page into text and ask the LLM.

### Step 4: `stats`

`cli.py → stats()` runs some `COUNT(*) ... GROUP BY` queries (`db.stats()`) and prints:

- how many companies came from each source
- how many were found per ATS and per tier
- the companies that failed, and why

---

## 3. The files, one by one

### `config.py`: settings

This file only holds constants: the database path, the User-Agent, the rate limits, the list of
blocked domains and the LLM model. Some can be overridden with environment variables. If you want
to change "how polite" the crawler is, this is the only file to touch.

### `models.py`: the shapes of our data

We use **Pydantic** models. They are like Python classes that check their own types, so
`Company(team_size="ten")` raises an error instead of silently storing bad data.

| Model | What it is |
|---|---|
| `Company` | One startup, with all its fields (YC info, careers page, ATS, crawl status) |
| `JobDraft` | **The one normalized job shape** that every extractor returns |
| `Job` | A `JobDraft` after saving, with extra fields: `company_id`, `source_tier`, `first_seen_at`, `last_seen_at`, `is_active` |
| `AtsType` | An enum: `greenhouse` / `lever` / `ashby` / `workable` |

It also has three small helpers used everywhere:

- **`domain_of(url)`** turns `"https://www.Stripe.com/jobs"` into `"stripe.com"`. Domains are how
  we recognize the same company coming from YC and from HN.
- **`guess_is_internship(title)`** uses a regex with **word boundaries** (`\b`). That way
  "Software **Intern**" matches, but "**Intern**al Tools" and "**Intern**ationalisation" don't.
  A real Stripe job, "Backend Engineer, Internationalisation", is in the tests for exactly this reason.
- **`guess_remote(text)`** returns `True` if it sees "remote", and **`None` (unknown) otherwise,
  never `False`**. A page that doesn't mention remote doesn't mean the job isn't remote.

**`JobDraft.clean_up()` runs on every job, from every tier.** It is a Pydantic `model_validator`,
which means it runs automatically each time a `JobDraft` is created. Having it in one place means
every source gets cleaned exactly the same way:

| Rule | Example | Why it was added |
|---|---|---|
| Remote unknown, but location/title says "remote" → `True` ("not remote" → `False`) | "Remote, EMEA" | These jobs were missing from the Remote filter |
| Job type normalized to Full-time / Part-time / Internship / Contract / Temporary | "FULL_TIME", "Exempt: Full-Time" → Full-time | There were 30+ spellings, including leaked HN text |
| Intern word in the title or job type → internship | "Working Student", "Werkstudent", "Trainee", "stagiaire" | Non-US internships were being missed |
| Dates converted to UTC | `-04:00` → `Z` | Sorting by "Newest" was slightly wrong with mixed time zones |
| Extra spaces in titles removed | "AI   Engineer\n" | Tidier cards |

Also: **Ashby's `isRemote` is `true` for hybrid jobs**, so the Ashby adapter uses `workplaceType`
instead. That was why hybrid Paris jobs showed up under "Remote only".

> **Why `bool | None` (three states)?** For a student, "unknown" and "no" are very different.
> If we don't know whether a job is remote, we shouldn't hide it from someone who wants remote work.

### `scoring.py`: quality_score

- `batch_date("Summer 2024")` returns `date(2024, 6, 1)`. It also understands old-style names like `"S24"`.
- `recency_points()` gives 40 points if the batch is less than 2 years old, then fades linearly to 0 at 6 years.
- `quality_score()` adds up the points from the table in [section 2](#step-1-seed).
  Companies seen only on HN get a flat 30.

This logic is plain `if` statements on purpose, so you can explain any company's score.

### `http.py`: the polite web client

**Every** network request in the project goes through `PoliteClient.get(url)`. Here is what
happens on each call:

```
get(url)
 ├─ 1. blocked domain? (linkedin.com, wellfound.com, ...)  → raise FetchError("blocked_domain")
 ├─ 2. ONLY if config.RESPECT_ROBOTS is on (off by default):
 │      allowed by robots.txt?                              → else raise FetchError("robots_blocked")
 └─ 3. up to 4 attempts:
        ├─ wait until ≥1 s since our last request to this site (or the site's Crawl-delay)
        ├─ wait for a free slot (max 10 requests at once, across all sites)
        ├─ send the request
        ├─ 2xx/3xx → return the response ✅
        ├─ 404/403/... → raise immediately (retrying won't help)
        └─ 429/5xx/timeout → sleep 1s, 2s, 4s ("exponential backoff") and try again
```

- **`FetchError`** carries a short `label`, like `"http_404"`, `"timeout"` or `"robots_blocked"`.
  That label is what you later see in `stats`, so every failure has a reason.
- **robots.txt is off by default** (`config.RESPECT_ROBOTS`). You chose more data over obeying it.
  Turn it on with `SORT_SHERR_RESPECT_ROBOTS=1` in `.env`. When on, it follows the standard:
  - no robots.txt (404) means everything is allowed
  - a server error (5xx) means we assume nothing is allowed
- **Redirects are checked too.** If a careers link redirects to LinkedIn, we still refuse it.

### `storage/db.py`: all database code

We use plain `sqlite3` (built into Python). The **only** file that contains SQL is this one, so
moving to Postgres later means changing one file.

**Tables** (created by `SCHEMA` the first time you `connect()`):

- `companies`: list fields like `tags` are stored as JSON text, e.g. `'["AI","B2B"]'`.
- `jobs`: `UNIQUE (company_id, dedupe_key)` stops duplicates at the database level as well.
- `llm_cache`: maps a page's hash to the jobs the LLM found on it.

**Key functions:**

| Function | What it does |
|---|---|
| `connect(path)` | Opens the DB file and creates the tables if they're missing |
| `find_company(domain, name)` | Looks up a company by domain. Only uses the name when the domain is unknown (two different startups can share a name) |
| `upsert_company(company)` | "Update or insert". Merges with an existing row (rules below), recomputes the score, saves |
| `update_company(id, **fields)` | Sets a few columns, e.g. after discover |
| `list_companies(where, ...)` | Loads companies, best score first |
| `save_jobs(company_id, tier, jobs)` | Inserts new jobs, updates seen jobs, deactivates vanished ones ([section 5](#5-the-life-of-a-job-in-the-database)) |
| `deactivate_hn_posts()` | Marks last month's HN posts inactive before a new thread is loaded |
| `cache_get` / `cache_put` | Reads and writes the LLM cache |
| `stats()` | The counting queries behind the `stats` command |

**Merge rules in `upsert_company`.** These matter when YC and HN both describe the same company:

- **List fields** (`sources`, `tags`, `locations`) are **combined**.
- **The same source** (for example, re-running `seed` for YC) **can update** values, so `is_hiring` stays fresh.
- **A different source** (an HN post about a YC company) **only fills empty fields**. HN can add a
  `contact` email, but it can't overwrite YC's description.
- An empty value **never erases** an existing one.

### `sources/yc.py`: YC companies

This one is short: download the JSON, keep the `Active` companies, and convert each with `to_company()`.

### `sources/hn.py`: HN "Who is hiring?"

1. `fetch_latest_thread()` asks the HN Algolia API for the newest posts by the `whoishiring`
   account, picks the one titled "Ask HN: Who is hiring?", then downloads the whole thread.
2. `parse_thread()` runs `parse_post()` on every top-level comment.

**`parse_post()` step by step**, for this post:

```
Synteq | Growth Lead & Systems Administrator | Austin, TX | REMOTE (US) | Full-time
```

1. Split the comment into paragraphs (`<p>`) and take the **first one that contains `|`**. Some
   posts start with "UPDATE: ..." before the header.
2. Split that header on `|`. The first part is the **company**, with any `(...)` removed, so
   "Shepherd (Series B)" becomes "Shepherd".
3. Label every other part using keyword regexes, in this order:

   | Check | Regex | Example |
   |---|---|---|
   | URL or money? → skip | `URL_LIKE`, `MONEY` | `$120k`, `acme.com` |
   | remote / onsite / hybrid? | `REMOTE_WORDS`, `ONSITE_WORDS` | `REMOTE (US)` |
   | employment type? | `EMPLOYMENT_WORDS` | `Full-time` |
   | role? | `ROLE_WORDS` | `Growth Lead & ...` |
   | anything else, the first time | → location | `Austin, TX` |

4. **Extra signals from the whole post:**
   - an **email** (`find_email()`, which also decodes "jobs [at] acme [dot] com")
   - an **ATS link**: `detect_ats()` on all links, e.g. United Risk → `lever/auw`
   - whether the word **"intern"** appears anywhere
   - the company **website**: a URL in the header, or a link whose domain contains the company name
5. The result is an `HNPost`. Then:
   - `to_company(post)` makes a `Company` (`sources=["hn"]`, `is_hiring=True`). A deep link like
     `acme.com/jobs/devops` becomes the `careers_url`, and `acme.com` becomes the website.
   - `to_job(post)` stores the **post itself as a job** with **tier 0**. Its `apply_url` is the HN
     comment link, so you can always go back and read the original.

> This parser is a **heuristic** (a best guess). HN posts have no fixed format. About 10% have no
> `Company | ...` line and are skipped, and sometimes a wrong part lands in "location". The tests
> in `tests/test_hn.py` use real posts so you can see exactly what it gets right.

### `discovery/ats.py`: which ATS, and what slug?

There is one regex per ATS, with the slug as the captured group:

```python
(AtsType.ashby, re.compile(r"jobs\.ashbyhq\.com/" + SLUG))   # jobs.ashbyhq.com/mem0/... → "mem0"
```

`detect_ats(html, url)` runs all the patterns over the page HTML **and** the page URL (in case
the careers page redirected straight to the ATS). It counts how often each `(ats, slug)` pair
appears and returns **the most mentioned one**. A page might link to a partner's board once, but
to its own board on every job.

`NOT_A_SLUG` filters out path parts that look like slugs but aren't, like `embed` in
`boards.greenhouse.io/embed/job_board?for=acme`.

### `discovery/careers.py`: finding the careers page

`discover(client, company)` tries these steps in order and stops at the first that works:

0. Already have a `careers_url` (e.g. from an HN post)? Fetch it and check it for an ATS.
1. No website? Use the YC jobs page if it's a YC company, otherwise give up (`no_website`).
2. Fetch the homepage. Does it link to an ATS directly? → done (`ats`).
3. `find_careers_link()` looks for the best link (an ATS link beats everything, LinkedIn links are
   skipped). If there isn't one, try `/careers` and then `/jobs`.
4. Fetch that page. Is there an ATS on it? → `ats`. If not → `careers_page` (Tier 2/3 will read it).
5. Nothing found → `ycombinator.com/companies/{slug}/jobs` as a last resort (`yc_jobs_page`).
   YC allows this in its robots.txt, and its job pages have JSON-LD.

It returns a small `Discovery` dataclass: `status`, `careers_url`, `ats_type`, `ats_slug`.

### `extractors/ats/*.py`: Tier 1 adapters

Every adapter has the same two functions:

```python
def api_url(slug: str) -> str          # where to ask
def parse(data) -> list[JobDraft]      # turn that ATS's JSON into our shape
```

`parse()` is a **pure function**: JSON in, `JobDraft`s out, no network. That's what makes it easy
to test with saved files. `extractors/ats/__init__.py` holds the `ADAPTERS` dict (ATS → module)
and `fetch_ats_jobs()`, which does `client.get_json(api_url)` followed by `parse()`.

Each ATS names things differently. This is the mapping in one place:

| Our field | Greenhouse | Lever | Ashby | Workable |
|---|---|---|---|---|
| title | `title` | `text` | `title` | `title` |
| team | `departments[0].name` | `categories.team` | `team` | `department` |
| location | `location.name` | `categories.location` | `location` | `city, country` |
| remote | guessed from text | `workplaceType` | `isRemote` | `telecommuting` |
| employment_type | – | `categories.commitment` | `employmentType` | `employment_type` |
| description | `content` (HTML, escaped!) | `descriptionPlain` | `descriptionPlain` | – (not in API) |
| apply_url | `absolute_url` | `hostedUrl` | `jobUrl` | `url` |
| posted_at | `first_published` | `createdAt` (milliseconds!) | `publishedAt` | `published_on` |

A few quirks the code handles:

- **Lever** returns `{"ok": false}` with HTTP 200 (not a 404) for an unknown slug.
- **Ashby** has hidden jobs (`isListed: false`), which we skip.
- **Greenhouse** sends HTML as `&lt;p&gt;`, so `text.html_to_text()` unescapes it first.
- **Rippling** (`rippling.py`): `name` is the title, and each location has a `workplaceType`
  (`REMOTE` / `ON_SITE` / `HYBRID`). The job counts as remote only if *all* its locations are remote.
- **Dover** (`dover.py`) needs **two requests**. The slug first gives the company's internal id,
  and that id gives the jobs. So instead of `api_url()` it has its own `async fetch(client, slug)`,
  and `fetch_ats_jobs()` uses `fetch` whenever an adapter has one. Demo jobs (`is_sample`) are skipped.
- **Gem** (`gem.py`) looks almost exactly like Greenhouse: `absolute_url`, `departments`,
  `content`, plus a `location_type` (remote / hybrid / onsite).

> **Real lesson from Saturn.** Saturn's own careers page still listed 9 roles, but its Rippling API
> returned only 4. Opening the other 5 gave `job_not_found`: they were **closed**, and the website
> was out of date. That's why the ATS API is Tier 1. It's the source of truth.

### `extractors/jsonld.py`: Tier 2

Many sites add a block like this so Google Jobs can show their postings:

```html
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Full Stack Engineer", "datePosted": "2026-06-19", "jobLocation": {...}}
</script>
```

- **`find_job_postings(html)`** parses every JSON-LD block and walks through it recursively. It
  handles a single object, a list, and the `@graph` wrapper that WordPress uses. Broken JSON on
  someone's site is skipped instead of crashing.
- **`to_job(posting)`** maps schema.org fields to `JobDraft`. For example, `jobLocationType:
  "TELECOMMUTE"` means remote, and `employmentType: "INTERN"` means internship.
- **`child_links(html, url)`** handles listing pages, which usually have no JSON-LD while each job
  page does. It collects links that sit **under** the careers page: on `/careers`, it takes
  `/careers/backend-engineer`, but not `/blog`. It follows at most 15 per company. If the careers
  "page" is the homepage, it does nothing, since every link would count as a child.
- **`extract_jobs()`** tries the page itself first, then its child pages.

### `extractors/llm.py`: Tier 3

```
HTML ──html_to_markdown()──► text with [links](url)
        │
        ├─ almost no text? → don't call the LLM at all (saves free-tier requests)
        ├─ more than 100,000 chars? → cut it, and record that in the notes
        │
        ├─ hash = sha256(url + text)
        ├─ hash in llm_cache? → return the cached jobs (no LLM call, no cost) ✅
        └─ else → provider.extract_jobs(text) → save to cache → return
```

- **`html_to_markdown()`** deletes `<script>`, `<style>` and similar tags, replaces each link with
  `[text](absolute url)` so the LLM can return apply links, and removes blank lines.
- **`LLMJob` / `LLMJobList`** are the Pydantic schema we ask the LLM to fill.
  - It is a slightly simpler version of `JobDraft` (no dates).
  - `to_job_draft()` converts it to a `JobDraft`.
  - If the LLM left `is_internship` empty, our own keyword check fills it in.
- **`LLMProvider`** is a `Protocol`, meaning "anything with an `extract_jobs(markdown, url)` method".
  - `GeminiProvider` is the real one. It uses **Google Gemini's free tier** (model
    `gemini-3.5-flash-lite`, key `GEMINI_API_KEY`). It calls
    `client.aio.models.generate_content(...)` with `response_mime_type="application/json"` and
    `response_schema=LLMJobList`. That forces Gemini to answer in exactly our JSON shape, and the
    SDK hands it back already parsed as `response.parsed`.
  - Because the free tier allows only a few requests per minute, `GeminiProvider`:
    - holds an `asyncio.Lock`, so only one LLM call runs at a time
    - waits `LLM_MIN_INTERVAL` (6 s) between calls
    - on a **429** ("too many requests") or **500/503** ("Google is busy"), sleeps 30/60/90 s and retries
  - The tests use a `FakeProvider`, plus a `GeminiProvider` whose network call is replaced by a
    fake, so no network, no key and no cost.
  - To use a different LLM company, write one new class with that one method.
  - **Why Gemini and not Groq?** Both are free without a card. Groq's free tier allows only about
    6–12K tokens per minute, and a single careers page can be 5–25K tokens. Gemini's free tier
    allows far more tokens per minute.
- **The cache key includes the page text.** When a company edits its careers page, the hash
  changes and the LLM runs again. When nothing changed, it costs nothing.
- **No API key** means `default_provider()` returns `None`, and the pipeline records `tier3_skipped`.

### `extractors/pipeline.py`: putting the tiers together

`extract_jobs(client, company, conn, provider)` returns a `CrawlResult`
(`status`, `tier`, `jobs`, `error`, `found_ats`):

```
A. Tier 1  company has ats_type + ats_slug (from discover)?
             ├─ API worked → return (even with 0 jobs: an empty ATS board really means "no open roles")
             └─ API failed (e.g. 404, wrong slug) → note it, continue
           no careers_url? → "no_careers_page"

B. PLAIN PAGE (fast): fetch careers page with httpx, then from_page():
             ├─ ATS link inside the page?   → Tier 1
             ├─ JSON-LD?                    → Tier 2
             └─ LLM                         → Tier 3
           got jobs (or a real error like "tier3_failed")? → return

C. RENDERED PAGE (slow): open the same URL in headless Chrome (browser.py), so JavaScript runs,
           then from_page() again on what a human would see.
           Often this reveals an ATS widget (Rootly → Gem) or a job list (Spade).
           got jobs? → return

D. YC FALLBACK: YC company? read ycombinator.com/companies/{slug}/jobs (JSON-LD → Tier 2)
```

- **`from_page(html)`** is "Tiers 1→2→3 on one version of the page". It's called once on the plain
  HTML and once on the rendered HTML, so there's no duplicated logic.
- **`found_ats`**: when step B or C finds an ATS that discover missed, `cli.py` saves it on the
  company (`ats_type`, `ats_slug`). The next crawl then goes straight to Tier 1, with no browser
  and no LLM.
- The browser step runs **only when the plain page gave nothing**. It is slow (a few seconds per
  page), and at most 2 browsers run at once (`MAX_PARALLEL_BROWSERS`).

The **notes** collect why each earlier step didn't work. That is what you see in the
`crawl_error` column and in `stats`, for example:
`tier2 no JobPosting JSON-LD; rendered with browser; tier2 no JobPosting JSON-LD`.

### `extractors/browser.py`: the headless browser

- `available()` checks whether Playwright is installed.
- `render(url)` starts headless Chromium, opens the page, waits until the network is quiet (or
  15 s at most), and returns the final HTML **plus the HTML of every iframe**, since embedded job
  boards often live in iframes. Blocked domains are refused here too.
- It returns `None` if anything goes wrong, so the pipeline just continues without it.

### `extractors/text.py`: small helpers

- `html_to_text()` turns HTML (even escaped HTML) into plain text.
- `parse_date()` accepts `"2026-11-03T18:56:53Z"`, `"2026-05-29"` or Lever's epoch milliseconds,
  and returns a `datetime`, or `None` if it can't parse the value.

### `cli.py`: the commands

Built with **Typer**: each function decorated with `@app.command()` becomes a command, and its
parameters become `--options`.

| Command | What it does |
|---|---|
| `seed` | YC: fetch → sort by score → keep `--limit` best → `upsert_company` each. HN: calls `seed_hn()` (deactivate old posts → parse → upsert companies → save posts as tier-0 jobs) |
| `discover` | Loads companies with `discover_status IS NULL` (or all with `--redo`), runs `discover()` on **all of them concurrently**, saves each result as soon as it arrives |
| `crawl` | Loads discovered companies with `quality_score >= --min-score`, runs `extract_jobs()` concurrently, then `save_jobs` and `update_company` for each |
| `stats` | Prints the tables from `db.stats()` |

---

### `web/`: the website

Built with **FastAPI** (a Python web framework) and **Jinja2** templates (HTML with `{{ }}` blanks
that Python fills in). No JavaScript framework: every page is plain HTML made on the server.

```
browser ──GET /?internship=1──► app.py jobs_page()
                                   ├─ reads filters from the URL (?q=..&internship=1)
                                   ├─ db.search_jobs(...)        ← SQL lives in storage/db.py
                                   └─ templates/jobs.html        ← turns rows into HTML
        ◄────────── HTML page ─────┘
```

| File | Job |
|---|---|
| `web/app.py` | All routes. Each `@app.get(...)` function = one page. Filters are read from the URL and **checked** (`int_or()`, fixed lists), so bad values can't break the SQL. |
| `web/templates/base.html` | The frame every page shares (header + nav). Other templates `{% extends %}` it. |
| `web/templates/_macros.html` | Reusable HTML pieces, e.g. `job_card()` and the badges. |
| `web/templates/jobs.html`, `companies.html`, `company.html`, `admin.html` | One per page. |
| `web/static/style.css` | All styling. **Light only.** Two themes, picked by `<html data-theme="lion">` or `"classic"`. A theme only sets color **variables** (`--accent`, `--button`, ...), and every layout rule uses them, so both share one layout. Lion = neutral light base + gold highlights (a few `[data-theme="lion"] ...` rules at the bottom); Classic = the original blue. |
| `web/templates/base.html` → theme script | Before the page draws, a 3-line script reads your saved theme from `localStorage` (so it never flashes the wrong colors). The header button flips `data-theme` and saves the choice. |
| `web/runner.py` | Runs admin tasks in the background (below). |
| `storage/db.py` → `search_jobs()`, `search_companies()`, `get_company()`, `company_jobs()` | The read queries the pages use. |
| `scoring.py` → `score_breakdown()` | The "Why this score?" list on the company page. |

**How an admin button works** (e.g. *Crawl*):

1. The form `POST`s to `/admin/run` with `action=crawl` and the fields.
2. `build_commands()` turns the form into CLI arguments, e.g. `["crawl", "--min-score", "40"]`.
   Every value is checked: numbers are bounded, choices must be in a fixed list. Anything else is
   dropped, so a form can never run something unexpected.
3. `runner.start()` launches `python -m sort_sherr.cli crawl ...` as a **separate process**,
   the exact same command you'd type. A long crawl can't freeze the website.
4. Each line the command prints is saved in `task.log`.
5. The admin page has a tiny script that asks `/admin/task` every 2 seconds and shows the new log.
   When the task finishes, it reloads the page so the numbers are fresh.

Only one task runs at a time. History is kept in memory (restarting the server forgets it; the
data itself is in SQLite). SQLite runs in **WAL mode** (`PRAGMA journal_mode=WAL` in `db.connect`),
which lets the website read while a crawl is writing.

## 4. Async, explained simply

Fetching a web page means waiting about 0.5–2 seconds for the server. Fetching 1,000 pages one
after another would take a long time. **Async** lets Python start many requests and do something
useful while waiting.

```python
async def one(client, company):        # "async def" = this function can pause while waiting
    d = await discover_one(client, company)   # "await" = pause here until the page arrives
    ...

await asyncio.gather(*(one(client, c) for c in companies))   # start all of them, wait for all
```

The **polite** part is what stops this from flooding websites. Inside `PoliteClient`:

- **`asyncio.Semaphore(10)`** works like 10 tickets: a request must hold a ticket, so at most 10
  run at once.
- **One `asyncio.Lock` per website**, plus the `last_hit` time, means requests to the *same* site
  go one at a time, at least 1 second apart. *Different* sites can run in parallel.

So 20 companies on 20 different sites finish in a few seconds, while no single site ever gets more
than one request per second from us.

> SQLite isn't async, but that's fine. All the async tasks run in **one thread** and take turns,
> so two tasks never write to the database at the same time.

---

## 5. The life of a job in the database

`db.save_jobs(company_id, tier, jobs)` runs after each successful crawl of a company.

**1. Make a fingerprint (`dedupe_key`) for each job**

- If it has an apply URL: `"url:https://jobs.ashbyhq.com/mem0/c5d3..."`
- If not: `"title:software engineer intern|bangalore"`, using a lowercased title and location with
  the punctuation removed

**2. For each job found in this crawl:**

- **Fingerprint not in the DB** → INSERT, with `first_seen_at = last_seen_at = now`, `is_active = 1`.
- **Fingerprint already there** → UPDATE its details, `last_seen_at = now`, `is_active = 1`.

**3. Any job of this company that is still active but wasn't found this time** → `is_active = 0`.
The role was filled or removed. We keep the row for history.

Example over three days:

| Day | ATS shows | What happens |
|---|---|---|
| Mon | Engineer, Designer | 2 inserted (new=2) |
| Tue | Engineer, Designer | 2 updated, `last_seen_at` moves (new=0) |
| Wed | Engineer | Engineer updated, **Designer → inactive** (gone=1) |

**HN posts (tier 0) are tracked separately from crawled jobs (tiers 1–3).** Crawling a company
never hides its HN post, and loading a new month's thread first marks all old HN posts inactive
(`deactivate_hn_posts()`).

**Only successful crawls call `save_jobs`.** If a crawl *fails* (timeout, blocked), we don't mark
jobs inactive. A failed fetch doesn't mean the jobs are gone.

---

## 6. Tests

Run them with `pytest`. There are **101 tests, and none use the internet**. They rely on
**fixtures**: real API responses saved into `tests/fixtures/` (trimmed to a few jobs).

| Test file | What it checks |
|---|---|
| `test_greenhouse.py`, `test_lever.py`, `test_ashby.py`, `test_workable.py`, `test_rippling.py`, `test_dover.py`, `test_gem.py` | Each adapter's `parse()` on a real saved response, plus each ATS's quirks |
| `test_jsonld.py` | A real YC JobPosting, the `@graph` format, broken JSON, child-link logic |
| `test_ats_detect.py` | ATS + slug detection on many URL shapes, careers-link picking, LinkedIn skipped |
| `test_hn.py` | Real HN posts: header parsing, UPDATE notes, emails, ATS links |
| `test_db.py` | Dedupe, inactive marking, merging YC + HN, same name with a different domain |
| `test_job_cleanup.py` | The shared clean-up rules (remote, internship words, job types, UTC dates, duplicates), using real cases from the app |
| `test_web.py` | Every page renders; filters work; bad URL values don't crash; admin password; admin forms turn into safe, checked commands |
| `test_llm.py` | Tier 3 with a **fake** LLM: conversion, cache hit, cache miss when the page changes; Gemini provider: parsed answer, raw-JSON fallback, waiting + retrying on a 429 |

`tests/conftest.py` defines `load_fixture`, a pytest fixture that reads a file from `tests/fixtures/`.

> **Why save real responses?** Live websites change every day, so a test that hits the network
> would randomly fail. Saved responses make tests fast, free and repeatable. When an ATS changes its
> format, save a new response and update the test.

---

## 7. Design decisions and why

| Decision | Why |
|---|---|
| **Tiers instead of per-site scrapers** | Thousands of sites; a few general methods cover most of them, and per-site code breaks constantly |
| **ATS first** | Official, structured, free data; no guessing |
| **One `JobDraft` shape for every tier** | The rest of the program (and Parts 2–4) never needs to care where a job came from |
| **Plain `sqlite3`, all SQL in one file** | No setup, easy to inspect; moving to Postgres later is a one-file change |
| **quality_score from YC signals** | No free funding data exists; batch recency is a fair stand-in for "recently funded" |
| **Keep companies with no jobs** | Many startups hire interns without posting it; Part 3 can still email them |
| **`None` = unknown** | Better to say "we don't know" than to hide a job wrongly |
| **Mark inactive, don't delete** | Keeps history (when a role opened and closed) |
| **Dropped trafilatura** | It's built for news articles and threw away job lists as "boilerplate"; our 15-line converter keeps them |
| **LLM behind a small interface + cache** | Easy to swap providers; never pay twice for the same page |
| **Failures stored with a reason** | `stats` can tell you *why* something failed, so you know what to fix next |
| **Blocklist + rate limit (robots.txt optional)** | Sites' terms (LinkedIn etc.) and IP bans are real risks; robots.txt is off by your choice to get more data |

---

## 8. How to extend it

**Add a new ATS, for example "Recruitee"**

1. Add `recruitee = "recruitee"` to `AtsType` in `models.py`.
2. Add a regex for its board URLs to `ATS_PATTERNS` in `discovery/ats.py`.
3. Create `extractors/ats/recruitee.py` with `api_url(slug)` and `parse(data)`.
4. Register it in the `ADAPTERS` dict in `extractors/ats/__init__.py`.
5. Save a real response as `tests/fixtures/recruitee.json` and write `tests/test_recruitee.py`.

**Use a different LLM:** write a class with
`async def extract_jobs(self, markdown, page_url) -> list[LLMJob]` and return it from
`default_provider()` in `extractors/llm.py`.

**Change how companies are ranked:** edit `quality_score()` in `scoring.py`, then re-run
`seed` so the scores are recomputed.

---

## 9. Try it yourself

These are small experiments that help you understand the code:

```bash
source .venv/bin/activate

# Watch one company go through every step
python -m sort_sherr.cli seed --limit 5
python -m sort_sherr.cli discover --limit 5
python -m sort_sherr.cli crawl --limit 5
python -m sort_sherr.cli stats

# See why each company scored what it did
sqlite3 -header -column data/sort_sherr.db \
  "SELECT name, yc_batch, stage, team_size, is_hiring, top_company, quality_score
   FROM companies ORDER BY quality_score DESC LIMIT 10"

# Run crawl twice: the second time every job should be 'new=0' (dedupe working)
python -m sort_sherr.cli crawl --limit 5

# Try the ATS detector on any HTML you like
python -c "from sort_sherr.discovery.ats import detect_ats; \
print(detect_ats('<a href=\"https://jobs.lever.co/spotify/123\">x</a>'))"

# Run one test file with details
pytest tests/test_hn.py -v
```

A good way to learn the code: pick one test, break the function it tests on purpose (for example,
remove the `\b` from `INTERN_PATTERN` in `models.py`), run `pytest`, and read which test fails and why.
