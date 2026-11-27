# Sort Sherr

Sort Sherr helps college students find **off-campus internships at good startups**, with
YC-backed and recently funded companies first.

The full project has four parts. **All four are built.** New here? Start with the
[end-to-end walkthrough](#how-to-use-sort-sherr-end-to-end).

| # | Part | Status |
|---|---|---|
| 1 | **Ingestion**: collect startups and their open roles | ✅ Built |
| 2 | **Resume parsing + matching** (embeddings + LLM reranking) | ✅ Built ([jump to Part 2](#part-2-resume--matching)) |
| 3 | **Company research + personalized email drafts** (drafts only, you send) | ✅ Built ([jump to Part 3](#part-3-company-research--email-drafts)) |
| 4 | **Outreach tracker + reply rate** (sent / replied / follow-ups / what works) | ✅ Built ([jump to Part 4](#part-4-outreach-tracker--reply-rate)) |

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

> ⚠️ On the free tier, Google may use what you send to improve its products. Part 1 only sends
> public careers pages. Part 2 sends your resume **with your name, email, phone, links and ID
> numbers removed first, on your computer** (see [Part 2](#part-2-resume--matching)).

**"503 UNAVAILABLE / high demand".** Sometimes a free model is overloaded for a while. The app
retries 3 times; if it still fails, run the same command later (finished work is cached), or try
the other free model for one run: `SORT_SHERR_LLM_MODEL=gemini-3.5-flash python -m sort_sherr.cli ...`

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

Everything is in one SQLite file, `data/sort_sherr.db`:

| Table | One row per | Key columns |
|---|---|---|
| `companies` | startup | name, website, yc_batch, team_size, is_hiring, quality_score, careers_url, ats_type, crawl_status |
| `jobs` | job posting | title, location, remote, is_internship, apply_url, source_tier, first_seen_at, last_seen_at, is_active |
| `llm_cache` | page the LLM has read | content_hash, jobs_json |
| `candidate_profiles` | parsed resume (Part 2) | resume_hash, profile_json, pii_json (**stays local**), model |
| `match_runs` | `match` run | candidate_id, prefs_json, filter_stats, embed_model, llm_model, llm_calls, tokens, cost_usd |
| `matches` | company in a run's pool | run_id, company_id, job_id, found_by, retrieval/rerank/final score, sub_scores, why_fit, why_this_role, reasons, red_flags |
| `embeddings` | embedded text | content_hash, model, vector |
| `rerank_cache` | LLM verdict on one company | cache_key, result_json |

To see active internships, best companies first:

```bash
sqlite3 -header -column data/sort_sherr.db "
  SELECT c.name, c.yc_batch, j.title, j.location, j.apply_url
  FROM jobs j JOIN companies c ON c.id = j.company_id
  WHERE j.is_active = 1 AND j.is_internship = 1
  ORDER BY c.quality_score DESC;"
```

---

## Part 2: resume → matching

You give it your resume. It gives back the ~15 startups that fit you best, each with **why it
suits you**, the best open role (if any) with **why that role**, evidence that pairs one thing from
your resume with one fact about the company, and red flags.

```
resume.pdf ──► text ──► remove name/email/phone/links/IDs (locally) ──► Gemini ──► profile (saved)
                                                                                     │
  ~4,600 companies ──► hard filters ──► embeddings (local model) ──► top 75 (+25 from your interests)
                                                                                     │
                                        Gemini scores each on a rubric, 5 at a time ◄┘
                                                                                     │
                                        final score = 0.85 × LLM + 0.10 × similarity + small boosts
```

### Setup (once)

```bash
pip install -e ".[match]"     # pypdf, python-docx, numpy, sentence-transformers (pulls in torch, ~1 GB)
mkdir -p uploads              # put your resume here: uploads/ is in .gitignore
```

The first `match` downloads the embedding model (`BAAI/bge-small-en-v1.5`, ~130 MB, runs on your
computer) and embeds every company (~1–2 minutes). After that, only changed companies are re-embedded.

### On the website (easiest)

```bash
python -m sort_sherr.cli serve      # then open http://127.0.0.1:8000/match  ("Match me" in the menu)
```

1. **Upload** your resume (PDF/DOCX). It's saved in `uploads/` under a hash, never sent as-is.
2. **Confirm** your name (pre-filled from the resume; it's removed before the LLM sees anything) and
   your graduation year.
3. On your profile page, see **suggested roles** (from your focus areas and skills, pre-ticked),
   choose **how many companies to shortlist** (15 / 30 / 50 / 75 / 100 / 150; each option shows
   the LLM calls and minutes it will take), write your interests, and press *Find my startups*.
4. Watch the progress live. The results page shows, for each company: why it suits you, the best
   **posted role** with an Apply button, or the **role to ask for** in a cold email when nothing
   fitting is posted, evidence, red flags and a score breakdown. **Download CSV** gives you the
   whole shortlist as a spreadsheet for applying.

"Quick mode" skips the LLM: instant and free, but no reasons. The Match pages need **no password**
(only `/admin` does). That's fine because the server only listens on this computer (127.0.0.1); don't
make the site reachable from a network, since anyone who can open it can see the uploaded resumes.

### From the terminal

```bash
# 1. Parse your resume (prints the profile + your candidate id). It asks two questions:
#    your name (BEFORE sending, so it can be removed: every template puts it somewhere else)
#    and your graduation year (AFTER parsing: resumes often don't state it).
python -m sort_sherr.cli parse-resume uploads/resume.pdf
python -m sort_sherr.cli parse-resume uploads/resume.pdf --name "First Last" --grad-year 2028   # no questions
# Same file again = no LLM call; use it to fix the year later.

# 2. Match. Preferences come from flags, not from the resume.
python -m sort_sherr.cli match --candidate 1
python -m sort_sherr.cli match --candidate 1 --interests "LLM agents and dev tools, small team, not crypto"
python -m sort_sherr.cli match --candidate 1 --role ai-ml --role backend --remote-only --window "Summer 2027"
python -m sort_sherr.cli match --candidate 1 --no-rerank        # embeddings only: free, instant, no LLM

# 3. Everything about one match: sub-scores, evidence, red flags, and exactly what the models saw
python -m sort_sherr.cli explain --candidate 1 --company 42
```

| Flag | What it does |
|---|---|
| `--interests "..."` / `--interests-file f.txt` | In your own words: what you like and what you're looking for. Adds up to 25 extra companies that match it, and the LLM scores "interest fit". Sent to the LLM as written, so keep personal details out. |
| `--role` | `any` (default), `sde`, `backend`, `frontend`, `fullstack`, `ai-ml`, `data`, `mobile`, `devops`, `design`, `product`. Repeat for several. |
| `--remote-only` | Drops companies **known** to be on-site only. Unknown is kept. |
| `--location India` | Drops companies **known** to be elsewhere (unless they have a remote role). Unknown is kept. |
| `--paid-only`, `--window "Summer 2027"` | Our data rarely says these, so they don't filter. The LLM flags concerns as red flags. |
| `--top 50` | Shortlist size (up to 150). The LLM scores ~1.5× this many (at least 75) so it can push weak ones out; every scored company is saved. |

**What the scores mean.** The LLM gives 0–20 for each of: stack overlap, domain fit (your projects
vs. what they build), interest fit, seniority fit (would they take an intern?), hiring signal
(intern posting > engineering posting > "is hiring"). Total 0–100. All weights are in
`sort_sherr/match_config.py`.

**Every run is saved** (`match_runs` + `matches`), with the models used and token counts, so you
can compare runs after changing preferences or weights.

### Is the LLM rerank worth it? (eval)

```bash
python -m sort_sherr.cli eval --candidate 1 --make-template --labels labels/me.csv
# open labels/me.csv, write good / ok / bad in the label column for each company
python -m sort_sherr.cli eval --candidate 1 --labels labels/me.csv
```

It prints **precision@15** (share of the top 15 you marked good) and **nDCG@15** (also rewards
putting the best ones highest) for *retrieval only* vs. *retrieval + rerank*, from the same run,
plus what the rerank cost in tokens. `labels/` is in `.gitignore`.

### Privacy

- Your resume stays on your computer (`uploads/`, `data/`, both in `.gitignore`).
- Before the LLM call, your **name, email, phone, URLs and long ID numbers** are cut out locally and
  stored only in the `pii_json` column. Your name is guessed (a name-like line matching your email
  or LinkedIn/GitHub username, else the first line), and you **confirm it before anything is sent**.
- The resume text is never logged or printed. Only the parsed profile is shown.

## Part 3: company research → email drafts

For your top matches, Sort Sherr reads the company's public pages, finds who to write to (with the
page each contact came from), and writes **two short email drafts** per company. You pick one, edit
it, and **send it yourself**. There is no sending code anywhere in this project.

```
 top matches (Part 2)
   └─► research: homepage, about/blog/changelog pages, careers page, YC page (founders, Launch YC,
       news headlines), Hacker News stories, their HN hiring post
         └─► LLM brief: what they build, who for, tech, recent updates (dated), hiring areas, people
             every fact keeps its SOURCE URL; facts citing a page we didn't read are dropped
   └─► contacts: emails / founder LinkedIn + X / apply page, each with "found at <url>"
   └─► 2 drafts: A = product-led opening, B = project-led opening
         └─► checks: rules + an LLM fact-check + lint → rewritten once if needed, else "needs_review"
   └─► review: pick a/b → edit in $EDITOR → approve → drafts/<company>.txt
```

### On the website (easiest)

`python -m sort_sherr.cli serve`, then **Match me → your resume → ✉ Email drafts**
(or the **✉ Draft emails** button on a shortlist).

- **Drafts page:** start a run (how many top matches, your availability, remote/onsite, signature
  links). It shows the daily cap and which companies are drafted, approved or contacted.
- **One company:**
  - The suggested recipient comes with a **"found here"** link to the page it was found on.
    All contacts are listed, each with a "Forget" button.
  - Both variants appear side by side in editable boxes, with the facts each one used (source links)
    and any check or lint notes.
  - **Approve this one** saves your edited text (lint runs on it again). **Copy** puts subject + body
    on your clipboard, and **Download .txt** saves it.
  - After approving, **I sent it** (channel + date) logs the send: the company is never drafted again
    and Part 4's follow-up clock starts. Without an approved draft, **I've contacted them** just marks it.

As with the rest of the site, the pages only read the database. Every change runs the normal CLI
command (`draft`, `approve-draft`, `sent`, `mark-contacted`, `forget-contact`).

### Commands

```bash
# Draft for your top matches (latest reranked run). Asks for your availability if not given.
python -m sort_sherr.cli draft --candidate 1 --top 5 --setup remote --window "Summer 2027"
# Signature links (saved locally once; never sent to the LLM). Without them, drafts have no link lines.
python -m sort_sherr.cli draft --candidate 1 --github https://github.com/you --portfolio https://you.dev --redo

# Go through them: pick a variant, edit it, approve. Saved to drafts/<company>.txt (+ --copy to clipboard)
python -m sort_sherr.cli review --candidate 1 --copy

# Sent it? Log it (Part 4). Wrote to a company some other way? mark-contacted. Either way it is never drafted again:
python -m sort_sherr.cli sent 12 --channel email
python -m sort_sherr.cli mark-contacted --candidate 1 --company 4

# One company at a time
python -m sort_sherr.cli research --company 4      # the brief, every fact with its source
python -m sort_sherr.cli contacts --company 4      # ranked contacts with their source pages
python -m sort_sherr.cli forget-contact --company 4 --id 7   # delete one (or all, without --id)
```

### How a draft is built

- **Inputs:** your parsed resume as numbered items (S1, S2…), the brief's facts (F1, F2…), Part 2's
  match reasons, and the ask: your availability, remote/onsite, and the role. The role is the posted
  role only if it is an internship; otherwise it's the intern role Part 2 suggested.
- **Rules** are in `sort_sherr/outreach/prompts/draft_email.md`:
  - 90–150 words, and a specific subject of at most 8 words
  - one sourced observation about them
  - ONE of your projects tied to what they build
  - one small ask
  - plain text, no flattery or buzzwords
- **Your name and links are never sent to the LLM.** It writes `[NAME]`, `[GITHUB]`, `[LINKEDIN]` and
  `[PORTFOLIO]`, and they are filled in on your computer afterwards.
- **Grounding check:**
  - Rules: the cited ids must exist, at least one company fact must be cited, and every number must appear in a source.
  - A second LLM pass matches each claim about the company or about you to a fact or resume item.
  - Any problem → the draft is rewritten once. Still wrong → `needs_review`. `review` shows which facts each
    draft used, with their links.
- **Lint** (also run after you edit):
  - word count and subject length
  - a generic subject ("Software Engineering Intern Role")
  - one block of text with no paragraphs
  - banned phrases (`prompts/banned_phrases.txt`)
  - unfilled placeholders
  - "too similar" to your draft for another company
- **Guardrails:**
  - at most **10 companies a day** (`SORT_SHERR_DAILY_DRAFT_CAP`)
  - companies you marked contacted are skipped, and so are companies already drafted (`--redo` to redraft)

### Who to write to (contacts)

| Source | What | Type |
|---|---|---|
| Their website, YC page, their HN posts | emails, founders' LinkedIn/X links, phone (own site only) | first-party |
| A founder's own website (the one their GitHub profile names) | work email, their own GitHub/LinkedIn/X | first-party (medium) |
| Other people's HN stories / news pages about the company | work email (company domain only), founders' profile links | third-party |
| GitHub API (only profiles their pages link to) | public work email, website, X handle | third-party |
| Hunter.io API (optional, `HUNTER_API_KEY`, free plan) | emails with their sources; the domain's pattern | api |

- An email **on the company's domain** is always kept.
- An email on **another domain** (gmail, a university…) is kept only if **the company itself published
  it** on its own pages. Many small startups use gmail. Such emails are marked "not on the company domain".
- An off-domain email found only on GitHub, Hunter, a founder's personal site or someone else's page is someone's personal address. It is **not collected**.
- Hunter's pattern guesses are labeled **UNVERIFIED GUESS** and are never the default recipient.
- Emails are checked with a DNS MX lookup (is there a mail server?) only. There is no SMTP probing.
- LinkedIn, X, Apollo, RocketReach and ZoomInfo are never fetched. A LinkedIn/X profile URL that a
  company page links to is stored as a link.

The **default recipient** is chosen in this order:
1. first-party work email
2. API-verified email
3. third-party email
4. LinkedIn/X DM
5. apply page

These rank below DMs:
- inboxes we can't place (research@, data@…)
- sales, support and similar inboxes
- domains with no mail server

A founder's LinkedIn is a better bet than any of them.

**Always open the "found at" link before sending.**

### Do the drafts look good? (eval-drafts)

```bash
python -m sort_sherr.cli eval-drafts --candidate 1          # LLM judge, fixed rubric (prompts/judge.md)
# open labels/draft_ratings.csv and fill my_rating (1-5) for each draft
python -m sort_sherr.cli eval-drafts --candidate 1 --report # judge vs you: mean difference, within 1 point, correlation
```

The judge scores specificity, grounding, relevance, length and tone from 1 to 5. Every Part 3 command
logs its LLM calls and tokens in the `outreach_runs` table. Part 3 settings live in
`sort_sherr/outreach_config.py`.

### Privacy (Part 3)

- Contacts, briefs and drafts live in `data/sort_sherr.db`. Approved emails are saved in `drafts/`.
  Both are in `.gitignore`.
- `forget-contact` deletes a contact and remembers the value so it is never saved again.
- Only public pages go to the LLM. Your name, email and links never do.

## Part 4: outreach tracker + reply rate

The core metric of Sort Sherr is **reply rate**. Part 4 tracks every email from draft to outcome,
tells you when to follow up, writes short follow-up drafts that add something new, and shows what
actually gets replies. It still **never sends anything**: you send from your own inbox and log it.

```
 drafted ──► sent ──► replied (positive / neutral / negative) ──► interview ──► offer / rejected
               ├──► bounced          └─ no reply: follow-up 1 (5 business days) ──► follow-up 2 (7 more)
               └──► withdrawn                                      └─ still nothing (7 more) ──► ghosted
```

Every change is saved as an **event with a timestamp** (not just the latest status), so we can compute
"days to reply" and "which message got the reply".

### On the website (easiest)

**Match me → your resume → 📬 Outreach tracker** (also linked from the drafts pages).

- **Outreach:** the **Today** list (interviews coming up, replies to answer, follow-ups due, most urgent
  first) and a table of every company you wrote to, with its status and what's next.
- **One company:** a timeline of everything that happened, plus forms to **log a reply**, **set a status**
  (only the moves that make sense are offered), **draft a follow-up**, approve it and mark it sent.
- **Reply rate & stats:** the funnel and every breakdown, each group with its **n**. Groups under 10 are
  flagged `n<10`, and no "winner" is named until every group compared has at least 10. Download the
  HTML report or a CSV from there.
- **Shortlists** (Part 2) show a badge on companies you already wrote to, and a small "your outreach so
  far" box (reply rate by team size, batch and recipient). It's information only: the ranking doesn't use it.

### Commands

```bash
python -m sort_sherr.cli sent 12 --channel email --at "2026-11-20 10:30"   # you sent draft #12
python -m sort_sherr.cli outreach --candidate 1        # everything, with ids and what's next
python -m sort_sherr.cli due --candidate 1             # what to do TODAY (also marks silent ones ghosted)
python -m sort_sherr.cli followup 3 --value="shipped PDF export for PaperPal"   # draft follow-up for outreach #3
python -m sort_sherr.cli sent 31                        # you sent follow-up draft #31 (same command)
python -m sort_sherr.cli reply 3 --type positive --note="wants a call next week"
python -m sort_sherr.cli status 3 answered              # you wrote back to their reply
python -m sort_sherr.cli status 3 interview --when "2026-12-03 15:00"
python -m sort_sherr.cli status 3 offer                 # or rejected / ghosted / bounced / withdrawn
python -m sort_sherr.cli outreach-stats --candidate 1   # reply rate + breakdowns in the terminal
python -m sort_sherr.cli report --candidate 1 --out reports/outreach.html   # one HTML file, open in a browser
python -m sort_sherr.cli export --candidate 1 --out exports/outreach.csv    # one row per outreach
python -m sort_sherr.cli forget --company 4             # delete ALL personal data about a company
python -m sort_sherr.cli forget --contact=ravi@acme.dev # delete one address everywhere
```

### Follow-ups

- **When:** 5 business days (Mon–Fri) after the first email with no reply, then 7 business days after
  follow-up 1 was *actually sent*. At most 2. Seven business days after the last one, it's ghosted.
  Any reply or bounce stops this at once. The numbers live in `sort_sherr/tracker_config.py`.
- **What:** one short email (25–60 words) in the same thread (`Re: <your subject>`) that adds **one new
  thing**: something you give with `--value` (a project you shipped, something you tried with their
  product), or else a company fact your first email didn't use, newest first.
- **Checks:** the same as Part 3 (cited ids, every number from a source, the LLM fact-check, lint). Lint
  also rejects "bumps" like "just following up" or "any update?" (`prompts/followup_banned_phrases.txt`).
- Follow-up drafts count toward the same **daily cap** as new drafts.

### What the numbers mean

- **Reply rate** = replies ÷ emails that reached someone (bounces are excluded).
- **Positive reply rate**, **interview rate**, and **median days to reply** are also shown.
- **Breakdowns:**
  - email variant (product-led vs project-led)
  - recipient role (founder / hiring or eng lead / hiring inbox / general inbox)
  - contact type and source (first-party / third-party / API)
  - match score bucket
  - YC batch year
  - team size
  - which message got the reply (first email / follow-up 1 / 2)
  - day of the week sent
  - channel
- With only a few emails, rates jump around: 1 reply out of 3 is 33%. That's why every number shows
  its n, and why "too few to judge" is the usual verdict at first.

### Privacy (Part 4)

- Outreach history lives in `data/sort_sherr.db` (gitignored). Store a short note, not their email text.
- Reports and CSVs are written only where you say (`--out`). `reports/` and `exports/` are gitignored.
  The CSV has no contact addresses or notes.
- No reply text is sent to any LLM. A follow-up's `--value` is redacted (links, emails, your name) before
  the LLM sees it, like your resume.
- `forget` deletes a company's contacts, drafts, outreach history and `drafts/` files, or one contact
  everywhere. Forgotten values are never saved again.
- There is no Gmail integration (yet): logging is manual, which works the same for email, LinkedIn, X
  and apply forms.

## How to use Sort Sherr, end to end

Each step builds on the one before. Steps 3 onwards also work on the website (`serve`, then **Match me**).

```bash
# 0. once: install (see Setup) and put a free GEMINI_API_KEY in .env
python -m sort_sherr.cli seed --source yc && python -m sort_sherr.cli seed --source hn   # 1. load startups
python -m sort_sherr.cli discover && python -m sort_sherr.cli crawl                      # 2. find their jobs
python -m sort_sherr.cli parse-resume uploads/me.pdf                                     # 3. your profile -> candidate id
python -m sort_sherr.cli match --candidate 1 --top 15 --interests "dev tools, agents"     # 4. your shortlist
python -m sort_sherr.cli draft --candidate 1 --top 5 --window "Summer 2027"              # 5. research + 2 drafts each
python -m sort_sherr.cli review --candidate 1 --copy                                     # 6. pick, edit, approve
#    ... send the email YOURSELF, from your own inbox ...
python -m sort_sherr.cli sent <draft id>                                                 # 7. log it
python -m sort_sherr.cli due --candidate 1                                               # 8. every day: what to do
python -m sort_sherr.cli followup <outreach id>                                          #    a follow-up is due
python -m sort_sherr.cli reply <outreach id> --type positive                             #    they answered!
python -m sort_sherr.cli outreach-stats --candidate 1                                    # 9. what works
python -m sort_sherr.cli report --candidate 1 --out reports/outreach.html
```

A good rhythm: run `due` each morning, draft for a few new companies (the daily cap is 10), and look at
`outreach-stats` once you have 10+ emails per group.

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
│   ├── cli.py             ← commands: seed, discover, crawl, stats, serve,
│   │                         parse-resume, match, explain, eval,
│   │                         research, contacts, forget-contact, draft, review, mark-contacted, eval-drafts,
│   │                         sent, reply, status, outreach, due, followup, outreach-stats, report, export, forget
│   ├── match_config.py    ← Part 2: every matching number (top-K, weights, boosts, token prices)
│   ├── matching/          ← Part 2
│   │   ├── models.py      ← CandidateProfile, Preferences, RerankResult
│   │   ├── resume.py      ← PDF/DOCX → text → redact → LLM → profile
│   │   ├── embed.py       ← Embedder interface, local model, embedding cache
│   │   ├── retrieve.py    ← hard filters + cosine top-K
│   │   ├── rerank.py      ← rubric prompt, batches, guards, cache
│   │   ├── rank.py        ← final score formula
│   │   ├── run.py         ← one match run, start to finish
│   │   ├── roles.py       ← suggested roles from focus areas / skills
│   │   └── evaluate.py    ← precision@K, nDCG@K, label CSV
│   ├── outreach_config.py ← Part 3: every research/draft number (cap, refresh age, word limits)
│   ├── outreach/          ← Part 3 (drafts only, never sends)
│   │   ├── models.py      ← Page, CompanyBrief (facts with source URLs), Contact, DraftPair
│   │   ├── research.py    ← pick + fetch public pages → LLM brief → drop unsourced facts → cache
│   │   ├── contacts.py    ← contacts from those pages, GitHub API, optional Hunter; MX check; ranking
│   │   ├── draft.py       ← 2 variants → fill your links locally → grounding check → one rewrite
│   │   ├── lint.py        ← word count, subject, banned phrases, placeholders, similarity
│   │   ├── run.py         ← one `draft` run: daily cap (follow-ups count), skip companies already written to
│   │   ├── judge.py       ← LLM-as-judge + your ratings CSV + agreement report
│   │   └── prompts/       ← brief.md, draft_email.md, followup_email.md, grounding_check.md, judge.md,
│   │                         banned_phrases.txt, followup_banned_phrases.txt
│   ├── tracker_config.py  ← Part 4: follow-up days, max follow-ups, small-n threshold, buckets
│   ├── tracker/           ← Part 4 (tracks; never sends)
│   │   ├── lifecycle.py   ← statuses, allowed moves, sent / reply / status
│   │   ├── followups.py   ← business days, next action, `due`
│   │   ├── followup_draft.py ← one short follow-up with one new thing, Part 3's checks
│   │   ├── metrics.py     ← reply rate, breakdowns with n, CSV, match feedback
│   │   ├── report.py      ← self-contained HTML report
│   │   └── templates/report.html
│   ├── web/               ← the website: app.py (routes), match_pages.py (Match me), draft_pages.py (drafts),
│   │                         outreach_pages.py (tracker), runner.py (tasks),
│   │                         templates/, static/
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
- **SQLite + numpy for now.** ~4,600 vectors fit in memory easily, so no vector DB yet. Postgres +
  pgvector later if the data grows.
- **Matching is only as good as the company data.** Most YC companies have a one-line description
  and no crawled jobs, so the LLM often has little to go on. Run `crawl` on more companies to help.
- **Gemini free tier can be overloaded** (503) for a while. Re-run later; finished work is cached.
- **Research only reads what's linked.** It reads the homepage, up to 5 of the company's own pages, the YC page and HN. It does no
  open web search for news, podcasts or talks: there is no free search API whose terms allow it.
- **Contacts are often generic inboxes.** Few startups list a founder's email. Expect the founder's LinkedIn/X
  (from the YC page) or an inbox like hello@/research@ as the best option, and judge it yourself.
- **No Gmail integration.** Approved drafts go to `drafts/*.txt` and the clipboard; you paste and send,
  then log sends and replies by hand (`sent`, `reply`, or the website).
- **Business days skip weekends only**, not public holidays.
