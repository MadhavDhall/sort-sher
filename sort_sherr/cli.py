"""Command line: seed -> discover -> crawl -> stats.

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
    for n, m in enumerate(rows, 1):
        batch = f"YC {m['yc_batch']}" if m["yc_batch"] else "non-YC"
        typer.echo(f"{n:>2}. {m['company_name']}  (company {m['company_id']}, {batch})  "
                   f"score {m['final_score']}  sim {m['retrieval_score']:.3f}  found by {m['found_by']}")
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


if __name__ == "__main__":
    app()
