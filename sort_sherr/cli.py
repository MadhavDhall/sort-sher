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
