"""Command line.

Part 1: seed -> discover -> crawl -> stats
Part 2: parse-resume -> match -> explain / eval
Part 3: draft -> review (+ research, contacts, forget-contact, mark-contacted, eval-drafts)
Part 4: sent -> due -> followup -> reply / status -> outreach-stats / report / export (+ outreach, forget)

Run with:  python -m sort_sherr.cli --help
"""

import asyncio

import typer

from . import config
from .http import PoliteClient
from .scoring import quality_score
from .storage import db

app = typer.Typer(help="Sort Sherr ingestion: collect startups and their open roles.", no_args_is_help=True)


@app.command()
def seed(
    source: str = typer.Option("yc", help="yc or hn"),
    limit: int = typer.Option(0, help="Keep only the N best companies by quality_score (0 = all)"),
    hiring_only: bool = typer.Option(False, help="YC: only companies marked isHiring"),
):
    """Load companies into the database."""

    if source == "hn":
        return seed_hn(limit)
    if source != "yc":
        raise typer.BadParameter(f"unknown source {source!r}")

    async def run():
        from .sources.yc import fetch_yc_companies

        async with PoliteClient() as client:
            return await fetch_yc_companies(client, hiring_only=hiring_only)

    companies = asyncio.run(run())
    companies.sort(key=quality_score, reverse=True)  # best first, so --limit keeps the best
    if limit:
        companies = companies[:limit]

    conn = db.connect(config.DB_PATH)
    for c in companies:
        db.upsert_company(conn, c)
    typer.echo(f"Seeded {len(companies)} companies from {source}.")


def seed_hn(limit: int):
    """Latest HN 'Who is hiring?' thread -> companies (+ each post stored as a tier-0 job)."""
    from .sources import hn

    async def run():
        async with PoliteClient() as client:
            return await hn.fetch_latest_thread(client)

    thread = asyncio.run(run())
    posts = hn.parse_thread(thread)
    if limit:
        posts = posts[:limit]

    conn = db.connect(config.DB_PATH)
    db.deactivate_hn_posts(conn)
    by_company: dict[int, list] = {}
    merged_with_yc = 0
    for post in posts:
        existing = db.find_company(conn, hn.domain_of(post.website), post.company)
        if existing and "yc" in existing.sources:
            merged_with_yc += 1
        cid = db.upsert_company(conn, hn.to_company(post))
        by_company.setdefault(cid, []).append(hn.to_job(post))
    for cid, jobs in by_company.items():
        db.save_jobs(conn, cid, 0, jobs)

    skipped = len(thread.get("children", [])) - len(posts) if not limit else "n/a"
    typer.echo(f"Thread: {thread.get('title')}")
    typer.echo(f"Parsed {len(posts)} posts into {len(by_company)} companies ({merged_with_yc} matched a YC company).")
    typer.echo(f"Skipped (no 'Company | ...' header line): {skipped}")


@app.command()
def discover(
    limit: int = typer.Option(0, help="Only the N best companies (0 = all)"),
    redo: bool = typer.Option(False, help="Also re-check companies already discovered"),
    source: str = typer.Option("", help="Only companies from this source (yc / hn)"),
):
    """Find each company's careers page and detect its ATS."""
    from .discovery.careers import discover as discover_one

    conn = db.connect(config.DB_PATH)
    where = "1=1" if redo else "discover_status IS NULL"
    if source:
        where += f" AND sources LIKE '%\"{source}\"%'"
    companies = db.list_companies(conn, where, limit=limit or None)
    typer.echo(f"Discovering careers pages for {len(companies)} companies...")

    async def one(client, c):
        d = await discover_one(client, c)
        db.update_company(
            conn,
            c.id,
            discover_status=d.status,
            careers_url=d.careers_url,
            ats_type=d.ats_type.value if d.ats_type else None,
            ats_slug=d.ats_slug,
        )
        typer.echo(f"  {c.name:<28} {d.status:<16} {d.ats_type.value + '/' + d.ats_slug if d.ats_type else d.careers_url or ''}")

    async def run():
        async with PoliteClient() as client:
            await asyncio.gather(*(one(client, c) for c in companies))

    asyncio.run(run())


@app.command()
def crawl(
    limit: int = typer.Option(0, help="Only the N best companies (0 = all)"),
    min_score: int = typer.Option(40, help="Skip companies below this quality_score"),
    source: str = typer.Option("", help="Only companies from this source (yc / hn)"),
    status: str = typer.Option("", help="Only companies whose last crawl had this status, e.g. tier3_skipped"),
):
    """Extract jobs for discovered companies (best quality_score first)."""
    from .extractors.llm import default_provider
    from .extractors.pipeline import extract_jobs

    conn = db.connect(config.DB_PATH)
    where = "discover_status IS NOT NULL AND quality_score >= ?"
    params: tuple = (min_score,)
    if source:
        where += f" AND sources LIKE '%\"{source}\"%'"
    if status:
        where += " AND crawl_status = ?"
        params += (status,)
    companies = db.list_companies(conn, where, params, limit or None)
    provider = default_provider()
    typer.echo(f"Crawling {len(companies)} companies... (tier 3 LLM: {'on, ' + config.LLM_MODEL if provider else 'off, no GEMINI_API_KEY'})")

    async def one(client, c):
        r = await extract_jobs(client, c, conn, provider)
        if r.status in ("ok", "no_jobs"):
            counts = db.save_jobs(conn, c.id, r.tier, r.jobs)  # also marks vanished jobs inactive
            interns = sum(1 for j in r.jobs if j.is_internship)
            detail = f"tier {r.tier}: {len(r.jobs)} jobs ({interns} intern) new={counts['new']} gone={counts['deactivated']}"
        else:
            detail = r.error or ""
        if r.found_ats:  # remember it, so the next crawl goes straight to Tier 1
            db.update_company(conn, c.id, ats_type=r.found_ats[0].value, ats_slug=r.found_ats[1], discover_status="ats")
            detail += f" [found ATS {r.found_ats[0].value}/{r.found_ats[1]}]"
        db.update_company(conn, c.id, crawl_status=r.status, crawl_error=r.error, last_crawled_at=db.now())
        typer.echo(f"  {c.name:<28} {r.status:<16} {detail}")

    async def run():
        async with PoliteClient() as client:
            await asyncio.gather(*(one(client, c) for c in companies))

    asyncio.run(run())


@app.command()
def stats():
    """Show counts per source / ATS / tier, and the latest failures."""
    s = db.stats(db.connect(config.DB_PATH))

    def table(title, rows):
        typer.echo(f"\n{title}")
        for row in rows or [("(none)",)]:
            typer.echo("  " + "  |  ".join(str(x) for x in row))

    typer.echo(f"Companies: {s['companies']}")
    typer.echo(
        f"Jobs: {s['jobs_active']} active ({s['internships_active']} internships), {s['jobs_inactive']} inactive"
    )
    table("Companies by source", s["companies_by_source"])
    table("Discover status", s["discover_status"])
    table("ATS detected", s["ats_type"])
    table("Crawl status", s["crawl_status"])
    table("Companies per tier (0=HN post, 1=ATS API, 2=JSON-LD, 3=LLM)", s["companies_by_tier"])
    table("Active jobs per tier", s["jobs_by_tier"])
    table("Failures (name | status | detail)", s["failures"])


# ---------- Part 2: resume parsing + matching ----------


def need_llm():
    from .extractors.llm import default_provider

    provider = default_provider()
    if provider is None:
        typer.echo("This needs a free GEMINI_API_KEY in .env (see README).", err=True)
        raise typer.Exit(1)
    return provider


@app.command("parse-resume")
def parse_resume(
    file: str = typer.Argument(..., help="Your resume, .pdf or .docx (keep it in uploads/)"),
    name: str = typer.Option("", help="Your name (removed before the LLM call); skips the question"),
    grad_year: int = typer.Option(0, help="Your graduation / passing-out year, e.g. 2028; skips the question"),
    yes: bool = typer.Option(False, "--yes", help="Don't ask; accept the detected name and year"),
):
    """Read a resume into a structured profile and save it. Prints the candidate id.

    Asks you two things (skip the questions with --name / --grad-year, or --yes to accept guesses):
      - your name, BEFORE anything is sent, so it can be removed (every template puts it elsewhere)
      - your graduation year, AFTER parsing (resumes often don't state it; the LLM may guess wrong)
    Running it again on the same file asks again and fixes the saved profile, with no LLM call."""
    from pathlib import Path

    from .matching import resume

    path = Path(file)
    if not path.is_file():
        raise typer.BadParameter(f"no such file: {file}")
    conn = db.connect(config.DB_PATH)
    already = db.get_candidate_by_hash(conn, resume.file_hash(path))

    provider = None  # already parsed: no LLM needed
    if not already:
        try:
            guess = resume.guess_name(path)
        except ValueError as e:
            raise typer.BadParameter(str(e))
        if not name and not yes:
            name = typer.prompt("Your full name, exactly as on the resume (it is removed before the LLM sees anything)",
                                default=guess or "", show_default=bool(guess)).strip()
        name = name or guess or ""
        if len(name.split()) < 1 or len(name) < 3:
            raise typer.BadParameter('need your name to remove it: pass --name "First Last"')
        provider = need_llm()
        typer.echo("Removing your name, email, phone, links and ID numbers locally, then sending the rest to the LLM...")
    try:
        cid, profile, cached = asyncio.run(resume.parse_resume(conn, provider, path, name or None))
    except ValueError as e:
        raise typer.BadParameter(str(e))

    found = resume.grad_year(profile)
    if not grad_year and not yes:
        answer = typer.prompt("Graduation (passing-out) year", default=str(found) if found else "", show_default=bool(found))
        grad_year = int(answer) if answer.strip().isdigit() else 0
    if grad_year:
        if not 2000 <= grad_year <= 2040:
            raise typer.BadParameter("graduation year should look like 2028")
        if grad_year != found:
            profile = resume.set_grad_year(profile, grad_year)
            db.update_candidate_profile(conn, cid, profile.model_dump_json())

    typer.echo(f"\nCandidate #{cid}" + ("  (already parsed, loaded from the database)" if cached else ""))
    print_profile(profile)


def print_profile(p) -> None:
    line = lambda label, value: value and typer.echo(f"  {label:<13} {value}")  # noqa: E731
    line("Name", p.name)
    for e in p.education:
        line("Education", ", ".join(x for x in [e.degree, e.branch, e.college, e.grad_date and f"grad {e.grad_date}"] if x))
    line("Focus", ", ".join(p.focus_areas))
    line("Skills", ", ".join(p.skills))
    line("Links", ", ".join(p.links))
    for pr in p.projects:
        typer.echo(f"  Project       {pr.title}  [{', '.join(pr.tech_stack)}]")
        for extra in (pr.description, pr.impact and f"impact: {pr.impact}"):
            if extra:
                typer.echo(f"                  {extra}")
    for e in p.experience:
        typer.echo(f"  Experience    {e.role} @ {e.organization or '?'} ({e.start or '?'} - {e.end or '?'})  [{', '.join(e.tech_stack)}]")
    for a in p.achievements:
        line("Achievement", a)


@app.command()
def match(
    candidate: int = typer.Option(..., help="Candidate id from parse-resume"),
    top: int = typer.Option(15, min=1, max=150, help="Shortlist size: how many companies you want (the LLM reranks ~1.5x this)"),
    interests: str = typer.Option("", help="In your own words: what you like and what you're looking for"),
    interests_file: str = typer.Option("", help="Same as --interests, read from a text file"),
    role: list[str] = typer.Option(["any"], help="Repeat for several: any/sde/backend/frontend/fullstack/ai-ml/data/mobile/devops/design/product"),
    remote_only: bool = typer.Option(False, help="Drop companies known to be on-site only"),
    location: list[str] = typer.Option([], help="Repeat for several, e.g. --location India --location Remote"),
    paid_only: bool = typer.Option(False, help="Flag companies with no sign of paid internships"),
    window: str = typer.Option("", help='When you can intern, e.g. "Summer 2027"'),
    no_rerank: bool = typer.Option(False, help="Retrieval only: no LLM calls, pure embedding order"),
):
    """Rank startups for a candidate: filters -> embeddings -> LLM rerank. Every run is saved."""
    from pathlib import Path

    from .matching.embed import SentenceTransformerEmbedder
    from .matching.models import ROLE_CHOICES, Preferences
    from .matching.run import run_match

    bad = [r for r in role if r not in ROLE_CHOICES]
    if bad:
        raise typer.BadParameter(f"unknown role(s) {bad}; choose from {ROLE_CHOICES}")
    if interests_file:
        interests = Path(interests_file).read_text().strip()
    if interests and not no_rerank:
        typer.echo("Note: your interests text is sent to the LLM as written. Keep personal details out of it.")
    prefs = Preferences(
        roles=role, remote_only=remote_only, locations=location, paid_only=paid_only,
        window=window or None, interests=interests or None, shortlist=top,
    )
    provider = None if no_rerank else need_llm()
    conn = db.connect(config.DB_PATH)
    try:
        run_id = asyncio.run(run_match(conn, SentenceTransformerEmbedder(), provider, candidate, prefs, typer.echo))
    except ValueError as e:
        raise typer.BadParameter(str(e))
    except Exception as e:
        if type(e).__module__ != "google.genai.errors":
            raise
        # Gemini overloaded (503) or out of free quota (429) even after retries.
        typer.echo(f"\nGemini error: {str(e)[:200]}", err=True)
        typer.echo("Finished rerank batches are cached: run the same command again later and it continues.", err=True)
        raise typer.Exit(1)
    print_run(conn, run_id, top)


def print_run(conn, run_id: int, top: int) -> None:
    run = db.get_run(conn, run_id)
    rows = db.run_matches(conn, run_id)[:top]
    typer.echo(f"\nRun #{run_id}: top {len(rows)} for candidate #{run['candidate_id']}"
               + ("" if run["reranked"] else "  (retrieval only, no LLM)") + "\n")
    contacted = db.outreach_status_by_company(conn, run["candidate_id"])  # Part 4: what you already did
    for n, m in enumerate(rows, 1):
        batch = f"YC {m['yc_batch']}" if m["yc_batch"] else "non-YC"
        you = f"  [outreach: {contacted[m['company_id']]}]" if m["company_id"] in contacted else ""
        typer.echo(f"{n:>2}. {m['company_name']}  (company {m['company_id']}, {batch})  "
                   f"score {m['final_score']}  sim {m['retrieval_score']:.3f}  found by {m['found_by']}{you}")
        if m["why_fit"]:
            typer.echo(f"    Why it suits you: {m['why_fit']}")
        if m["job_title"]:
            typer.echo(f"    Best role: {m['job_title']}" + (f" -- {m['why_this_role']}" if m["why_this_role"] else ""))
            if m["apply_url"]:
                typer.echo(f"               {m['apply_url']}")
        elif m["pitch_role"]:
            typer.echo(f"    Ask for: {m['pitch_role']}  (no matching posting)")
        for e in m["reasons"]:
            typer.echo(f"      - you: {e['resume_item']}  <->  them: {e['company_fact']}")
        if m["red_flags"]:
            typer.echo(f"    Red flags: {'; '.join(m['red_flags'])}")
        if not run["reranked"]:
            c = db.get_company(conn, m["company_id"])
            typer.echo(f"    {(c.description or '').strip()[:140]}")
        typer.echo("")
    if run["reranked"]:
        typer.echo(f"LLM usage: {run['llm_calls']} calls, {run['prompt_tokens']:,} input + {run['output_tokens']:,} "
                   f"output tokens. Cost: $0 on the free tier (~${run['cost_usd']:.4f} at paid prices).")
    typer.echo(f"Details for one company: python -m sort_sherr.cli explain --candidate {run['candidate_id']} --company <id>")
    print_outreach_so_far(conn, run["candidate_id"])


def print_outreach_so_far(conn, candidate: int) -> None:
    """Part 4 feedback: which kinds of companies replied so far. Only shown; the ranking doesn't use it."""
    from . import tracker_config
    from .tracker.metrics import feedback, pct

    f = feedback(conn, candidate)
    if f is None:
        return
    typer.echo(f"\nYour outreach so far: {f['replies']} replies from {f['delivered']} delivered emails "
               "(shown for you; it does not change the ranking).")
    for title, groups in f["by"]:
        parts = [f"{g['group']} {pct(g['reply_rate'])} (n={g['n']})" for g in groups]
        typer.echo(f"  by {title}: " + ", ".join(parts))
    if f["delivered"] < tracker_config.SMALL_N:
        typer.echo(f"  (fewer than {tracker_config.SMALL_N} emails: too few to judge yet)")


@app.command()
def explain(
    candidate: int = typer.Option(..., help="Candidate id"),
    company: int = typer.Option(..., help="Company id (shown in the match list)"),
    run: int = typer.Option(0, help="Run id (default: the latest run that included this company)"),
):
    """Full breakdown of one match: every score, evidence, red flags and what the models saw."""
    from . import match_config as cfg
    from .matching.rank import boosts
    from .matching.rerank import company_card
    from .matching.retrieve import company_document

    conn = db.connect(config.DB_PATH)
    run_id = run or db.latest_run_for_company(conn, candidate, company)
    rows = [m for m in db.run_matches(conn, run_id) if m["company_id"] == company] if run_id else []
    if not rows:
        typer.echo("This company is not in any match run for this candidate (filtered out or not retrieved).")
        raise typer.Exit(1)
    m = rows[0]
    rank_no = [x["company_id"] for x in db.run_matches(conn, run_id)].index(company) + 1
    c = next(x for x in db.matching_companies(conn) if x["id"] == company)

    typer.echo(f"{m['company_name']}: rank {rank_no} in run #{run_id}  (prefs: {db.get_run(conn, run_id)['prefs_json']})\n")
    typer.echo(f"Found by:         {m['found_by']}")
    typer.echo(f"Retrieval score:  {m['retrieval_score']:.4f}  (cosine similarity)")
    typer.echo(f"Rerank score:     {m['rerank_score'] if m['rerank_score'] is not None else '(not reranked)'}  / 100")
    for k, v in m["sub_scores"].items():
        typer.echo(f"   {k:<15} {v:>2} / 20")
    typer.echo(f"Boosts:           +{boosts(c):.1f}  (recent batch up to {cfg.BOOST_RECENT_BATCH}, intern posting {cfg.BOOST_INTERN_POSTING})")
    typer.echo(f"Final score:      {m['final_score']}  = {cfg.W_RERANK} x rerank + {cfg.W_RETRIEVAL} x retrieval(0-100, scaled in pool) + boosts")
    if m["why_fit"]:
        typer.echo(f"\nWhy it suits you: {m['why_fit']}")
    if m["job_title"]:
        typer.echo(f"Best role: {m['job_title']} (job {m['job_id']}) -- {m['why_this_role'] or ''}\n  {m['apply_url'] or ''}")
    typer.echo("\nEvidence:")
    for e in m["reasons"] or [{"resume_item": "(none)", "company_fact": ""}]:
        typer.echo(f"  - you: {e['resume_item']}\n    them: {e['company_fact']}")
    typer.echo("\nRed flags: " + ("; ".join(m["red_flags"]) or "(none)"))
    typer.echo("\n--- what the embedding model saw ---\n" + company_document(c))
    typer.echo("\n--- what the LLM saw ---\n" + company_card(c))


@app.command("eval")
def eval_cmd(
    candidate: int = typer.Option(..., help="Candidate id"),
    labels: str = typer.Option("labels/labels.csv", help="Your hand labels: company_id,company_name,label (good/ok/bad)"),
    run: int = typer.Option(0, help="Run id (default: this candidate's latest reranked run)"),
    k: int = typer.Option(15, min=1, max=100, help="Measure the top K"),
    make_template: bool = typer.Option(False, help="Write the labels CSV with the companies to label, then stop"),
):
    """Precision@K and nDCG@K: retrieval only vs retrieval + rerank, against your labels."""
    from pathlib import Path

    from .matching import evaluate

    conn = db.connect(config.DB_PATH)
    run_id = run or db.latest_run_id(conn, candidate, reranked_only=True)
    if not run_id:
        typer.echo("No reranked run for this candidate yet: run `match` first.")
        raise typer.Exit(1)
    path = Path(labels)
    if make_template:
        n = evaluate.write_template(conn, run_id, path, k)
        typer.echo(f"Wrote {n} companies from run #{run_id} to {path}. Fill the label column with good / ok / bad.")
        return
    if not path.exists():
        typer.echo(f"No labels file at {path}. Create one with --make-template.")
        raise typer.Exit(1)
    gold = evaluate.load_labels(path)
    results = evaluate.evaluate(conn, run_id, gold, k)
    r = db.get_run(conn, run_id)
    typer.echo(f"Run #{run_id}, {len(gold)} labeled companies ({sum(1 for g in gold.values() if g == 2)} good)\n")
    typer.echo(f"  {'':<20} {'precision@' + str(k):>13} {'nDCG@' + str(k):>9} {'unlabeled in top ' + str(k):>20}")
    for name, m in results.items():
        typer.echo(f"  {name:<20} {m['precision']:>13.2f} {m['ndcg']:>9.3f} {m['unlabeled']:>20}")
    typer.echo(f"\nRerank cost for this run: {r['llm_calls']} LLM calls, {r['prompt_tokens'] + r['output_tokens']:,} tokens "
               f"($0 on the free tier, ~${r['cost_usd']:.4f} at paid prices).")
    if any(m["unlabeled"] for m in results.values()):
        typer.echo("Some top companies are unlabeled (counted as 0). Run with --make-template to add them to your CSV.")


@app.command()
def serve(
    port: int = typer.Option(8000, help="Port to listen on"),
    reload: bool = typer.Option(False, help="Restart automatically when code changes (for development)"),
):
    """Start the website: jobs, companies and the admin panel."""
    import uvicorn

    typer.echo(f"Sort Sherr is running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    # 127.0.0.1 = only reachable from this computer. The admin page can start crawls,
    # so don't expose it to a network without setting SORT_SHERR_ADMIN_PASSWORD.
    uvicorn.run("sort_sherr.web.app:app", host="127.0.0.1", port=port, reload=reload)


# ---------- Part 3: research, contacts, drafts (never sends anything) ----------


def run_llm(coro):
    """Run an async step; turn Gemini's 503/429 (after retries) into a short message."""
    try:
        return asyncio.run(coro)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    except Exception as e:
        if type(e).__module__ != "google.genai.errors":
            raise
        typer.echo(f"\nGemini error: {str(e)[:200]}", err=True)
        typer.echo("Finished briefs and drafts are saved: run the same command again later and it continues.", err=True)
        raise typer.Exit(1)


def need_company(conn, company_id: int):
    c = db.get_company(conn, company_id)
    if c is None:
        raise typer.BadParameter(f"no company #{company_id}")
    return c


@app.command()
def research(
    company: int = typer.Option(..., help="Company id (shown in the match list)"),
    refresh: bool = typer.Option(False, help="Fetch the pages again even if the saved brief is recent"),
):
    """Research one company from public pages and print the brief, every fact with its source."""
    from .matching.rerank import usage_cost
    from .outreach.research import research_company

    conn = db.connect(config.DB_PATH)
    c = need_company(conn, company)

    async def go():
        async with PoliteClient() as client:
            return await research_company(conn, client, need_llm(), company, refresh)

    r = run_llm(go())
    db.log_outreach_run(conn, "research", None, r["usage"], usage_cost(r["usage"]))
    print_brief(c.name, r)


def print_brief(name: str, r: dict) -> None:
    from .outreach.models import numbered_facts

    when = "saved brief from " + r["fetched_at"] if r["cached"] else "just researched"
    typer.echo(f"\n{name}  ({when}; {len(r['pages'])} pages read)\n")
    section = None
    for f in numbered_facts(r["brief"]):
        if f["section"] != section:
            section = f["section"]
            typer.echo(section.replace("_", " ").upper())
        typer.echo(f"  {f['id']}: {f['text']}" + (f"  ({f['date']})" if f.get("date") else ""))
        typer.echo(f"      source: {f['source_url']}")
    if r["brief"].people:
        typer.echo("PEOPLE")
        for p in r["brief"].people:
            typer.echo(f"  {p.name}" + (f", {p.role}" if p.role else "") + f"\n      source: {p.source_url}")
    typer.echo("\nPages read:")
    for p in r["pages"]:
        typer.echo(f"  [{p.kind}] {p.url}")
    if r["dropped"]:
        typer.echo(f"\nDropped {r['dropped']} fact(s)/person(s) whose source was not a page we read.")
    for note in r["notes"]:
        typer.echo(f"  could not read: {note}")


@app.command()
def contacts(
    company: int = typer.Option(..., help="Company id"),
    refresh: bool = typer.Option(False, help="Research the company again first"),
):
    """Find who to write to: every contact with the page it was found on. Check them before sending."""
    from .matching.rerank import usage_cost
    from .outreach import contacts as ct
    from .outreach.research import research_company

    conn = db.connect(config.DB_PATH)
    c = need_company(conn, company)

    async def go():
        async with PoliteClient() as client:
            r = await research_company(conn, client, need_llm(), company, refresh)
            found = await ct.find_contacts(conn, client, company, r["pages"], r["brief"], finder=ct.default_finder())
            return r, found

    r, found = run_llm(go())
    db.log_outreach_run(conn, "research", None, r["usage"], usage_cost(r["usage"]))
    print_contacts(c.name, found)


def print_contacts(name: str, found: list[dict], limit: int = 0) -> None:
    from .outreach.contacts import default_recipient, describe

    best = default_recipient(found)
    typer.echo(f"\nContacts for {name}, best first (open the 'found at' link to check before you send):")
    if not found:
        typer.echo("  (none found: look at their website / YC page yourself)")
    for n, c in enumerate(found[: limit or None], 1):
        mark = "-> " if best and c["id"] == best["id"] else "   "
        typer.echo(f"{mark}{n:>2}. [id {c['id']}] {describe(c)}")
    if best:
        typer.echo(f"\nDefault recipient: {best['value']}  (marked ->)")


@app.command("forget-contact")
def forget_contact(
    company: int = typer.Option(..., help="Company id"),
    id: int = typer.Option(0, "--id", help="Contact id to forget (from `contacts`); 0 = all of this company's contacts"),
):
    """Delete contacts (personal data) and never store those values again."""
    conn = db.connect(config.DB_PATH)
    need_company(conn, company)
    if not id and not typer.confirm(f"Forget ALL contacts of company #{company}?"):
        raise typer.Exit(0)
    n = db.forget_contacts(conn, company, id or None)
    typer.echo(f"Forgot {n} contact(s). They will not be saved again, even if a page still lists them.")


@app.command("mark-contacted")
def mark_contacted(
    candidate: int = typer.Option(..., help="Candidate id"),
    company: int = typer.Option(..., help="Company id you already wrote to"),
    note: str = typer.Option("", help="Optional note, e.g. 'emailed founder 26 Nov'"),
):
    """Remember that you already contacted a company: `draft` will never draft for it again."""
    conn = db.connect(config.DB_PATH)
    need_company(conn, company)
    db.mark_contacted(conn, candidate, company, note or None)
    typer.echo(f"Marked company #{company} as contacted.")


@app.command()
def draft(
    candidate: int = typer.Option(..., help="Candidate id"),
    top: int = typer.Option(10, min=1, max=50, help="Look at this many top matches (the daily cap still applies)"),
    run: int = typer.Option(0, help="Match run id (default: the latest reranked run)"),
    window: str = typer.Option("", help='When you can intern, e.g. "Summer 2027" (asked if not given)'),
    setup: str = typer.Option("", help="remote / onsite / either (default: from the match run)"),
    redo: bool = typer.Option(False, help="Draft again for companies that already have drafts"),
    github: str = typer.Option("", help="Your GitHub URL for the email signature (saved locally, never sent to the LLM)"),
    linkedin: str = typer.Option("", help="Your LinkedIn URL for the signature (saved locally)"),
    portfolio: str = typer.Option("", help="Your portfolio / resume URL for the signature (saved locally)"),
):
    """Research + contacts + 2 email drafts for your top matches. Drafts only: nothing is ever sent."""
    from .matching.models import Preferences
    from .matching.rerank import usage_cost
    from . import outreach_config
    from .outreach.contacts import default_finder
    from .outreach.run import SETUPS, draft_for_candidate, drafts_left_today

    conn = db.connect(config.DB_PATH)
    run_id = run or db.latest_run_id(conn, candidate, reranked_only=True)
    if not run_id or not db.get_run(conn, run_id):
        raise typer.BadParameter("no reranked match run for this candidate; run `match` first")
    prefs = Preferences.model_validate_json(db.get_run(conn, run_id)["prefs_json"])
    window = window or prefs.window or typer.prompt('When can you intern? (e.g. "Summer 2027", "May-July 2027")').strip()
    setup = setup or ("remote" if prefs.remote_only else "")
    if not setup:
        setup = typer.prompt("Work setup you want", default="either", type=typer.Choice(list(SETUPS)))
    if setup not in SETUPS:
        raise typer.BadParameter(f"--setup must be one of {list(SETUPS)}")
    if not window or len(window) > 120:
        raise typer.BadParameter("give your availability, e.g. --window \"Summer 2027\"")
    save_links(conn, candidate, [github, linkedin, portfolio])
    left = drafts_left_today(conn, candidate)
    typer.echo(f"Run #{run_id}, top {top}. Daily cap: {left} of {outreach_config.DAILY_DRAFT_CAP} companies left today.")

    async def go():
        async with PoliteClient() as client:
            return await draft_for_candidate(conn, client, need_llm(), candidate, top=top, window=window, setup=setup,
                                             run_id=run_id, redo=redo, finder=default_finder(), progress=typer.echo)

    out = run_llm(go())
    typer.echo("")
    for r in out["results"]:
        extra = f"  checks: {', '.join(r['checks'])}" if r.get("checks") else ""
        typer.echo(f"  {r['company']:<28} {r['status']}{extra}")
    u = out["usage"]
    typer.echo(f"\nLLM usage: {u['llm_calls']} calls, {u['prompt_tokens']:,} input + {u['output_tokens']:,} output tokens. "
               f"Cost: $0 on the free tier (~${usage_cost(u):.4f} at paid prices).")
    typer.echo(f"Next: python -m sort_sherr.cli review --candidate {candidate}")


def save_links(conn, candidate: int, urls: list[str]) -> None:
    """Add signature links to the candidate's local pii_json (once; later runs reuse them)."""
    import json

    urls = [u.strip() for u in urls if u.strip()]
    if not urls:
        return
    for u in urls:
        if not u.startswith("https://") or len(u) > 200 or any(ch.isspace() for ch in u):
            raise typer.BadParameter(f"not a https:// link: {u}")
    row = db.get_candidate(conn, candidate)
    if row is None:
        raise typer.BadParameter(f"no candidate #{candidate}")
    pii = json.loads(row["pii_json"])
    pii["links"] = pii.get("links", []) + [u for u in urls if u not in pii.get("links", [])]
    db.update_candidate_pii(conn, candidate, json.dumps(pii))
    typer.echo(f"Saved {len(urls)} link(s) for your signature (local only).")


@app.command()
def review(
    candidate: int = typer.Option(..., help="Candidate id"),
    copy: bool = typer.Option(False, help="Also copy each approved email to the clipboard (macOS pbcopy)"),
    all_: bool = typer.Option(False, "--all", help="Also show companies you already approved"),
):
    """Go through your drafts one company at a time: pick a variant, edit it, approve it.
    Approved emails are saved to drafts/<company>.txt for YOU to send. Nothing is sent from here."""
    import click

    from .outreach import contacts as ct
    from .outreach.lint import lint

    conn = db.connect(config.DB_PATH)
    drafts = db.candidate_drafts(conn, candidate)
    by_company: dict[int, list[dict]] = {}
    for d in drafts:
        by_company.setdefault(d["company_id"], []).append(d)
    todo = [ds for ds in by_company.values() if all_ or not any(d["chosen"] for d in ds)]
    if not todo:
        typer.echo("Nothing to review. Run `draft` first (or --all to see approved ones).")
        return

    for n, variants in enumerate(todo, 1):
        name, company_id = variants[0]["company_name"], variants[0]["company_id"]
        typer.echo("\n" + "=" * 78 + f"\n{n}/{len(todo)}  {name}  (company {company_id})\n" + "=" * 78)
        found = ct.rank_contacts(db.company_contacts(conn, company_id))
        print_contacts(name, found, limit=6)
        best = ct.default_recipient(found)
        for d in variants:
            print_variant(d)
        choice = typer.prompt("\nPick a variant: a / b, s = skip, q = quit", default="a").strip().lower()
        if choice == "q":
            break
        chosen = next((d for d in variants if d["variant"].lower() == choice), None)
        if chosen is None:
            continue
        others = [(d["company_name"], d["edited_body"] or d["body"]) for d in drafts if d["company_id"] != company_id]
        text = f"Subject: {chosen['subject']}\n\n{chosen['edited_body'] or chosen['body']}\n"
        while True:
            edited = click.edit(text, extension=".txt")
            text = edited if edited is not None else text
            subject, body = split_subject(text)
            flags = lint(subject, body, others)
            typer.echo("\n" + text.strip())
            typer.echo("\nLint: " + ("ok" if not flags else "; ".join(flags)))
            action = typer.prompt("[a]pprove, [e]dit again, [s]kip", default="a").strip().lower()
            if action != "e":
                break
        if action != "a":
            continue
        path = approve_and_export(conn, chosen, subject, body)
        typer.echo(f"Saved to {path}. Check the recipient's source link, then send it yourself.")
        if copy:
            copy_to_clipboard(f"Subject: {subject}\n\n{body}")


def approve_and_export(conn, d: dict, subject: str, body: str):
    """Save your edited version as the chosen draft for that company, and write drafts/<company>.txt.
    Part 4: approving a first email starts its outreach record ('drafted'); a follow-up is exported
    to drafts/<company>-followup-<n>.txt so the first email's file stays."""
    from .outreach import contacts as ct

    db.approve_draft(conn, d["id"], body, subject)
    best = ct.default_recipient(db.company_contacts(conn, d["company_id"]))
    if d["followup_number"]:  # same thread: to the contact you actually wrote to
        o = db.get_outreach(conn, d["outreach_id"])
        if o and o["contact_value"]:
            best = {"value": o["contact_value"], "person_name": o["contact_person"],
                    "person_role": o["contact_role"], "source_url": o["contact_source_url"]}
        return export_draft(d["company_name"], best, subject, body, suffix=f"-followup-{d['followup_number']}")
    db.ensure_outreach(conn, d["candidate_id"], d["company_id"], d["id"], best["id"] if best else None)
    return export_draft(d["company_name"], best, subject, body)


@app.command("approve-draft")
def approve_draft(
    draft_id: int = typer.Option(..., "--draft", help="Draft id"),
    subject: str = typer.Option(..., help="The subject you want (pass as --subject=...)"),
    body: str = typer.Option(..., help="The email body you want (pass as --body=...)"),
):
    """Approve one draft with your edits (used by the website; `review` does the same interactively)."""
    from .outreach.lint import lint

    conn = db.connect(config.DB_PATH)
    d = db.get_draft(conn, draft_id)
    if d is None:
        raise typer.BadParameter(f"no draft #{draft_id}")
    subject, body = " ".join(subject.split()), body.replace("\r\n", "\n").strip()
    if not subject or not body:
        raise typer.BadParameter("subject and body can't be empty")
    others = [(x["company_name"], x["edited_body"] or x["body"])
              for x in db.candidate_drafts(conn, d["candidate_id"]) if x["company_id"] != d["company_id"]]
    for flag in lint(subject, body, others, followup=bool(d["followup_number"])):
        typer.echo(f"lint: {flag}")
    path = approve_and_export(conn, d, subject, body)
    typer.echo(f"Approved draft #{draft_id} ({d['company_name']}, variant {d['variant']}); saved to {path}")


def print_variant(d: dict) -> None:
    typer.echo(f"\n--- Variant {d['variant']} ({d['style']})   check: {d['check_status']}" + (" (approved)" if d["chosen"] else ""))
    typer.echo(f"Subject: {d['subject']}\n")
    typer.echo(d["edited_body"] or d["body"])
    typer.echo("\n  Facts used:")
    for f in d["facts_used"] or [{"id": "-", "text": "(none)", "source_url": ""}]:
        typer.echo(f"    {f['id']}: {f['text']}" + (f"  ({f['date']})" if f.get("date") else "") + f"\n        {f['source_url']}")
    typer.echo("  Your resume items used: " + ("; ".join(f"{i['id']} {i['text'][:70]}" for i in d["student_items_used"]) or "(none)"))
    for note in d["check_notes"]:
        typer.echo(f"  CHECK: {note}")
    for flag in d["lint_flags"]:
        typer.echo(f"  LINT: {flag}")


def split_subject(text: str) -> tuple[str, str]:
    """'Subject: ...' on the first line, then the body."""
    lines = text.strip().splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        return lines[0].split(":", 1)[1].strip(), "\n".join(lines[1:]).strip()
    return "", text.strip()


def export_draft(company_name: str, recipient: dict | None, subject: str, body: str, suffix: str = ""):
    """drafts/<company>.txt: who to send it to (and where that address came from), subject, body."""
    import re

    from .outreach_config import DRAFTS_DIR

    from .outreach.draft import export_text

    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-") or "company"
    path = DRAFTS_DIR / f"{slug}{suffix}.txt"
    path.write_text(export_text(company_name, recipient, subject, body))
    return path


def copy_to_clipboard(text: str) -> None:
    import shutil
    import subprocess

    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=text.encode(), check=False)
        typer.echo("Copied to the clipboard.")
    else:
        typer.echo("(pbcopy not found: copy it from the file instead)")


@app.command("eval-drafts")
def eval_drafts(
    candidate: int = typer.Option(..., help="Candidate id"),
    n: int = typer.Option(10, min=1, max=30, help="How many drafts to judge"),
    report: bool = typer.Option(False, help="Compare the judge with your my_rating column instead of judging"),
    ratings: str = typer.Option("", help="CSV path (default labels/draft_ratings.csv)"),
):
    """LLM-as-judge on a fixed rubric + your own 1-5 ratings: does the judge agree with you?"""
    from pathlib import Path

    from .matching.rerank import usage_cost
    from .outreach import judge
    from .outreach_config import RATINGS_CSV

    path = Path(ratings) if ratings else RATINGS_CSV
    conn = db.connect(config.DB_PATH)
    if report:
        if not path.exists():
            typer.echo(f"No ratings file at {path}: run eval-drafts without --report first.")
            raise typer.Exit(1)
        r = judge.report(path)
        if not r["rated"]:
            typer.echo(f"No ratings yet: fill the my_rating column (1-5) in {path}.")
            raise typer.Exit(1)
        typer.echo(f"{r['rated']} of {r['total']} drafts rated by you\n")
        typer.echo(f"  {'draft':<7} {'company':<24} {'var':<4} {'judge':>6} {'you':>4}")
        for p in r["pairs"]:
            typer.echo(f"  {p['draft_id']:<7} {p['company'][:24]:<24} {p['variant']:<4} {p['judge']:>6.1f} {p['mine']:>4}")
        corr = "n/a (need 3+ ratings that vary)" if r["correlation"] is None else f"{r['correlation']:.2f}"
        typer.echo(f"\nAverage: judge {r['judge_mean']:.2f}, you {r['my_mean']:.2f}")
        typer.echo(f"Mean absolute difference: {r['mean_abs_diff']:.2f} points; within 1 point: {r['within_1']}/{r['rated']}; "
                   f"correlation: {corr}")
        return

    drafts = judge.pick_drafts(conn, candidate, n)
    if not drafts:
        typer.echo("No drafts yet: run `draft` first.")
        raise typer.Exit(1)
    provider = need_llm()
    usage = {"llm_calls": 0, "prompt_tokens": 0, "output_tokens": 0}

    async def go():
        scores = []
        for d in drafts:
            typer.echo(f"  judging {d['company_name']} {d['variant']}...")
            s, used = await judge.judge_draft(provider, d)
            usage["llm_calls"] += 1
            usage["prompt_tokens"] += used.get("prompt_tokens", 0)
            usage["output_tokens"] += used.get("output_tokens", 0)
            scores.append(s)
        return scores

    try:
        scores = run_llm(go())
    finally:
        db.log_outreach_run(conn, "judge", candidate, usage, usage_cost(usage))
    rows = judge.judge_rows(drafts, scores)
    judge.write_csv(path, rows)
    typer.echo(f"\n  {'draft':<7} {'company':<24} {'var':<4} " + " ".join(f"{c[:5]:>5}" for c in judge.CRITERIA) + "   avg")
    for r in rows:
        typer.echo(f"  {r['draft_id']:<7} {r['company'][:24]:<24} {r['variant']:<4} "
                   + " ".join(f"{r[c]:>5}" for c in judge.CRITERIA) + f"  {r['judge_avg']:.1f}  {r['judge_comment']}")
    typer.echo(f"\nWrote {path}. Fill in my_rating (1-5), then run with --report.")
    typer.echo(f"Judge cost: {usage['llm_calls']} calls, {usage['prompt_tokens'] + usage['output_tokens']:,} tokens "
               f"($0 on the free tier, ~${usage_cost(usage):.4f} at paid prices).")


# ---------- Part 4: outreach tracker (you send; Sort Sherr only keeps track) ----------


def tracker_call(fn, *args, **kwargs):
    """Run a lifecycle step; its ValueError (bad id, a move that isn't allowed) becomes a CLI error."""
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise typer.BadParameter(str(e))


@app.command()
def sent(
    draft_id: int = typer.Argument(..., help="The draft you sent (id from `review` / the website)"),
    channel: str = typer.Option("", help="email / linkedin / x / apply_form (default: from the contact)"),
    at: str = typer.Option("", help="When you sent it: YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (default: now)"),
    contact: int = typer.Option(0, help="Contact id you sent it to (default: the suggested recipient)"),
):
    """Log that YOU sent a draft (first email or follow-up). Starts the follow-up clock."""
    from .tracker.lifecycle import log_sent

    conn = db.connect(config.DB_PATH)
    oid, what = tracker_call(log_sent, conn, draft_id, channel or None, at or None, contact or None)
    o = db.get_outreach(conn, oid)
    typer.echo(f"Logged: {what} to {o['company_name']} via {o['channel']} (outreach #{oid}).")
    typer.echo(f"Got an answer? python -m sort_sherr.cli reply {oid} --type positive|neutral|negative")


@app.command()
def reply(
    outreach_id: int = typer.Argument(..., help="Outreach id (from `outreach` / `due`)"),
    type: str = typer.Option(..., "--type", help="positive / neutral / negative"),
    note: str = typer.Option("", help="Short note, e.g. 'wants a call next week' (pass as --note=...)"),
    at: str = typer.Option("", help="When they replied (default: now)"),
):
    """Log a reply. Follow-ups stop at once."""
    from .tracker.lifecycle import log_reply

    conn = db.connect(config.DB_PATH)
    tracker_call(log_reply, conn, outreach_id, type, note[:300] or None, at or None)
    typer.echo(f"Logged a {type} reply for outreach #{outreach_id}. No more follow-ups will be suggested.")


@app.command()
def status(
    outreach_id: int = typer.Argument(..., help="Outreach id"),
    new_status: str = typer.Argument(..., help="interview / offer / rejected / ghosted / bounced / withdrawn / answered"),
    when: str = typer.Option("", help="Interviews: the date and time, e.g. '2026-12-03 15:00'"),
    note: str = typer.Option("", help="Short note (pass as --note=...)"),
    at: str = typer.Option("", help="When it happened (default: now)"),
):
    """Move an outreach on: interview, offer, rejected... ('answered' = you wrote back to their reply)."""
    from .tracker.lifecycle import set_status

    conn = db.connect(config.DB_PATH)
    tracker_call(set_status, conn, outreach_id, new_status, note[:300] or None, at or None, when or None)
    typer.echo(f"Outreach #{outreach_id}: {new_status}.")


@app.command()
def outreach(candidate: int = typer.Option(..., help="Candidate id")):
    """Every company you wrote to (or approved a draft for): status, channel, what's next."""
    from datetime import date

    from .tracker.followups import local_date, next_action

    conn = db.connect(config.DB_PATH)
    events, today = db.candidate_events(conn, candidate), date.today()
    rows = db.candidate_outreach(conn, candidate)
    if not rows:
        typer.echo("No outreach yet. Approve a draft with `review`, send it yourself, then `sent <draft id>`.")
        return
    typer.echo(f"  {'id':>4}  {'company':<24} {'status':<20} {'channel':<10} {'sent':<11} next")
    for o in rows:
        ev = events.get(o["id"], [])
        sent_ev = next((e for e in ev if e["kind"] == "sent"), None)
        a = next_action(o, ev, today)
        nxt = "-" if a is None else (a["label"] if a["due_on"] <= today else f"{a['label']} on {a['due_on']}")
        status_text = o["status"] + (f" ({o['reply_type']})" if o["reply_type"] else "")
        typer.echo(f"  {o['id']:>4}  {o['company_name'][:24]:<24} {status_text:<20} {o['channel']:<10} "
                   f"{str(local_date(sent_ev['at'])) if sent_ev else '-':<11} {nxt}")


@app.command()
def due(candidate: int = typer.Option(..., help="Candidate id")):
    """What needs action TODAY, most urgent first: interviews, replies to answer, follow-ups."""
    from .tracker.followups import due_items

    conn = db.connect(config.DB_PATH)
    items = due_items(conn, candidate, apply_ghost=True)
    if not items:
        typer.echo("Nothing due today. Good moment to `draft` for new companies.")
        return
    typer.echo(f"{len(items)} thing(s) to do today:\n")
    for n, i in enumerate(items, 1):
        a = i["action"]
        late = f"  ({i['days_late']} day(s) late)" if a["kind"] == "followup" and i["days_late"] > 0 else ""
        typer.echo(f"{n:>2}. {i['company_name']}  (outreach #{i['id']}): {a['label']}{late}")
        if a["kind"] == "followup":
            if a.get("draft_id"):
                typer.echo(f"      draft #{a['draft_id']} is ready: send it yourself, then `sent {a['draft_id']}`")
            else:
                typer.echo(f"      write it: python -m sort_sherr.cli followup {i['id']} [--value \"what's new\"]")
        elif a["kind"] == "reply":
            typer.echo(f"      after you answer: `status {i['id']} answered` (or interview / rejected)")
        elif a["kind"] == "send" and i["draft_id"]:
            typer.echo(f"      after sending: `sent {i['draft_id']}`")


@app.command()
def followup(
    outreach_id: int = typer.Argument(..., help="Outreach id (from `due`)"),
    value: str = typer.Option("", help="The ONE new thing to add, e.g. 'shipped PDF export for PaperPal' (pass as --value=...)"),
    redo: bool = typer.Option(False, help="Write this follow-up again"),
    no_review: bool = typer.Option(False, help="Only save the draft (the website reviews it on its own page)"),
):
    """Draft a short follow-up (under 60 words) that adds one new piece of value. You review and send it."""
    import click

    from .matching.rerank import usage_cost
    from .outreach.lint import lint
    from .tracker.followup_draft import draft_followup
    from .tracker.lifecycle import need_outreach

    conn = db.connect(config.DB_PATH)
    o = tracker_call(need_outreach, conn, outreach_id)

    async def go():
        async with PoliteClient() as client:
            return await draft_followup(conn, client, need_llm(), outreach_id, value or None, redo)

    d, usage = run_llm(go())
    db.log_outreach_run(conn, "followup", o["candidate_id"], usage, usage_cost(usage))
    print_variant(d)
    if no_review:
        typer.echo(f"Saved follow-up draft #{d['id']}.")
        return
    text = f"Subject: {d['subject']}\n\n{d['body']}\n"
    while True:
        action = typer.prompt("\n[a]pprove, [e]dit, [s]kip (it stays saved as a draft)", default="a").strip().lower()
        if action == "e":
            text = click.edit(text, extension=".txt") or text
            subject, body = split_subject(text)
            typer.echo("\n" + text.strip() + "\nLint: " + ("; ".join(lint(subject, body, followup=True)) or "ok"))
            continue
        break
    if action == "a":
        subject, body = split_subject(text)
        path = approve_and_export(conn, d, subject or d["subject"], body)
        typer.echo(f"Saved to {path}. Send it IN THE SAME THREAD yourself, then: sent {d['id']}")


@app.command("outreach-stats")
def outreach_stats(candidate: int = typer.Option(..., help="Candidate id")):
    """Reply rate and what drives it (variant, recipient, follow-ups...). Every group shows its n."""
    from . import tracker_config
    from .tracker.metrics import pct, summary

    s = summary(db.connect(config.DB_PATH), candidate)
    typer.echo("Funnel: " + "  ->  ".join(f"{stage} {n}" for stage, n in s["funnel"]))
    typer.echo(f"Sent {s['sent']}, bounced {s['bounced']}, delivered {s['delivered']}.")
    typer.echo(f"Reply rate {pct(s['reply_rate'])}   positive {pct(s['positive_rate'])}   "
               f"interview {pct(s['interview_rate'])}   median days to reply "
               f"{s['median_days_to_reply'] if s['median_days_to_reply'] is not None else '-'}")
    if s["small"]:
        typer.echo(f"(Only {s['delivered']} delivered: below {tracker_config.SMALL_N}, treat every rate as an early signal.)")
    for b in s["breakdowns"]:
        typer.echo(f"\n{b['title']}")
        for g in b["groups"]:
            flag = f"  n<{tracker_config.SMALL_N}" if g["small"] else ""
            typer.echo(f"  {g['group'][:26]:<26} n={g['n']:<4} replies={g['replies']:<4} {pct(g['reply_rate']):>5}{flag}")
        typer.echo(f"  -> {b['verdict']}")


@app.command()
def report(
    candidate: int = typer.Option(..., help="Candidate id"),
    out: str = typer.Option(..., help="Where to save the HTML file, e.g. reports/outreach.html"),
):
    """Write a single self-contained HTML report (funnel + charts) you can open in a browser."""
    from pathlib import Path

    from .tracker.report import render_report

    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(db.connect(config.DB_PATH), candidate))
    typer.echo(f"Wrote {path}. Open it in your browser (it contains company names: it's personal, keep it local).")


@app.command()
def export(
    candidate: int = typer.Option(..., help="Candidate id"),
    out: str = typer.Option(..., help="Where to save the CSV, e.g. exports/outreach.csv"),
):
    """One CSV row per outreach (no contact addresses or notes), for your own analysis."""
    from pathlib import Path

    from .tracker.metrics import records, to_csv

    rows = records(db.connect(config.DB_PATH), candidate)
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_csv(rows))
    typer.echo(f"Wrote {len(rows)} row(s) to {path}.")


@app.command()
def forget(
    company: int = typer.Option(0, help="Delete ALL personal data about this company id"),
    contact: str = typer.Option("", help="Delete this email / handle / URL everywhere (pass as --contact=...)"),
    yes: bool = typer.Option(False, "--yes", help="Don't ask for confirmation"),
):
    """Delete personal data: contacts, drafts, outreach history (and drafts/ files) for a company, or one contact."""
    import re

    from .outreach_config import DRAFTS_DIR

    if bool(company) == bool(contact):
        raise typer.BadParameter("give exactly one of --company or --contact")
    conn = db.connect(config.DB_PATH)
    if company:
        c = need_company(conn, company)
        if not yes and not typer.confirm(f"Delete all contacts, drafts and outreach history for {c.name}?"):
            raise typer.Exit(0)
        counts = db.forget_company_data(conn, company)
        slug = re.sub(r"[^a-z0-9]+", "-", c.name.lower()).strip("-") or "company"
        files = [p for p in DRAFTS_DIR.glob(f"{slug}*.txt") if re.fullmatch(rf"{re.escape(slug)}(-followup-\d+)?\.txt", p.name)]
        for p in files:
            p.unlink()
        typer.echo(f"Forgot {c.name}: {counts['outreach']} outreach, {counts['drafts']} draft(s), "
                   f"{counts['contacts']} contact(s), {len(files)} file(s). Public company data stays.")
    else:
        if not yes and not typer.confirm(f"Forget {contact} everywhere?"):
            raise typer.Exit(0)
        n = db.forget_contact_value(conn, contact)
        typer.echo(f"Forgot {contact} ({n} saved row(s)). It will never be saved again.")


if __name__ == "__main__":
    app()
