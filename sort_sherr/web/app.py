"""The Sort Sherr website: browse jobs and companies, and an admin page to fetch new data.

Run it:   python -m sort_sherr.cli serve     then open http://127.0.0.1:8000

Pages (all HTML, made on the server with Jinja2 templates in web/templates/):
  /                  jobs, with search + filters
  /companies         companies, with search + filters
  /companies/{id}    one company: details, why it got its score, all its jobs
  /admin             stats + buttons to run seed / discover / crawl, with live output
JSON (used by the admin page's little script):
  /admin/task        the current task's status and log
"""

import secrets
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config
from ..models import AtsType
from ..scoring import score_breakdown
from ..storage import db
from .runner import runner

HERE = Path(__file__).parent
PAGE_SIZE = 50
TIER_NAMES = {0: "HN post", 1: "ATS API", 2: "JSON-LD", 3: "LLM"}
CRAWL_STATUSES = ["tier3_skipped", "tier3_failed", "no_jobs", "no_careers_page"]

app = FastAPI(title="Sort Sherr")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(TIER_NAMES=TIER_NAMES, ATS_TYPES=[a.value for a in AtsType])
templates.env.filters["day"] = lambda v: (v or "")[:10]  # "2026-11-03T18:56:53+00:00" -> "2026-11-03"


def get_conn():
    """A fresh DB connection per request (cheap for SQLite), closed afterwards."""
    conn = db.connect(config.DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


# ---------- admin password (optional) ----------

security = HTTPBasic(auto_error=False)


def require_admin(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    """If SORT_SHERR_ADMIN_PASSWORD is set, the admin pages ask for it (any username).
    If it isn't set, admin is open -- fine on your own laptop, NOT if you put the site online."""
    if not config.ADMIN_PASSWORD:
        return
    ok = credentials and secrets.compare_digest(credentials.password.encode(), config.ADMIN_PASSWORD.encode())
    if not ok:
        raise HTTPException(401, "Admin password required", headers={"WWW-Authenticate": "Basic"})


# ---------- helpers ----------


def int_or(value: str | None, default: int | None, lo: int = 0, hi: int = 1_000_000) -> int | None:
    """Turn a query/form value into a bounded int, or the default if it's empty/invalid."""
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def page_links(request: Request, page: int, total: int) -> dict:
    """URLs for the previous/next page, keeping all the current filters."""
    pages = max(1, -(-total // PAGE_SIZE))  # ceiling division

    def url(p):
        params = dict(request.query_params) | {"page": p}
        return f"{request.url.path}?{urlencode(params)}"

    return {
        "page": page, "pages": pages, "total": total,
        "prev": url(page - 1) if page > 1 else None,
        "next": url(page + 1) if page < pages else None,
    }


# ---------- public pages ----------


@app.get("/")
def jobs_page(request: Request, conn=Depends(get_conn)):
    p = request.query_params
    page = int_or(p.get("page"), 1, lo=1)
    filters = {
        "q": p.get("q", "").strip(),
        "internship": p.get("internship") == "1",
        # "remote" = only remote jobs, "onsite" = only on-site/hybrid, "" = any
        "remote": p.get("remote") if p.get("remote") in ("remote", "onsite") else "",
        "tier": int_or(p.get("tier"), None, 0, 3),
        "source": p.get("source", "") if p.get("source") in ("yc", "hn") else "",
        "min_score": int_or(p.get("min_score"), 0, 0, 100),
        "sort": p.get("sort", "score"),
    }
    jobs, total = db.search_jobs(conn, **filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    return templates.TemplateResponse(
        request, "jobs.html", {"jobs": jobs, "f": filters, "nav": page_links(request, page, total), "active": "jobs"}
    )


@app.get("/companies")
def companies_page(request: Request, conn=Depends(get_conn)):
    p = request.query_params
    page = int_or(p.get("page"), 1, lo=1)
    filters = {
        "q": p.get("q", "").strip(),
        "source": p.get("source", "") if p.get("source") in ("yc", "hn") else "",
        "ats": p.get("ats", "") if p.get("ats") in [a.value for a in AtsType] else "",
        "hiring": p.get("hiring") == "1",
        "has_jobs": p.get("has_jobs") == "1",
        "has_internships": p.get("has_internships") == "1",
        "min_score": int_or(p.get("min_score"), 0, 0, 100),
        "sort": p.get("sort", "score"),
    }
    companies, total = db.search_companies(conn, **filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    return templates.TemplateResponse(
        request, "companies.html",
        {"companies": companies, "f": filters, "nav": page_links(request, page, total), "active": "companies"},
    )


@app.get("/companies/{company_id}")
def company_page(request: Request, company_id: int, conn=Depends(get_conn)):
    company = db.get_company(conn, company_id)
    if company is None:
        raise HTTPException(404, "Company not found")
    jobs = db.company_jobs(conn, company_id)
    return templates.TemplateResponse(
        request, "company.html",
        {
            "c": company,
            "reasons": score_breakdown(company),
            "active_jobs": [j for j in jobs if j["is_active"]],
            "old_jobs": [j for j in jobs if not j["is_active"]],
            "active": "companies",
        },
    )


# ---------- admin ----------


@app.get("/admin", dependencies=[Depends(require_admin)])
def admin_page(request: Request, conn=Depends(get_conn)):
    return templates.TemplateResponse(
        request, "admin.html",
        {
            "s": db.stats(conn),
            "task": runner.current.to_dict() if runner.current else None,
            "history": [t.to_dict() for t in runner.history],
            "busy": runner.busy,
            "llm_on": bool(config.GEMINI_API_KEY),
            "llm_model": config.LLM_MODEL,
            "statuses": CRAWL_STATUSES,
            "active": "admin",
        },
    )


@app.get("/admin/task", dependencies=[Depends(require_admin)])
def admin_task():
    return JSONResponse({"busy": runner.busy, "task": runner.current.to_dict() if runner.current else None})


def build_commands(action: str, form: dict) -> tuple[str, list[list[str]]]:
    """Turn an admin form into CLI argument lists. Every value is checked against a fixed list or
    converted to a bounded int, so the form can never inject anything unexpected."""
    limit = int_or(form.get("limit"), 0, 0, 10_000)
    source = form.get("source") if form.get("source") in ("yc", "hn") else ""
    min_score = int_or(form.get("min_score"), 40, 0, 100)

    if action == "seed":
        args = ["seed", "--source", source or "yc"]
        if limit:
            args += ["--limit", str(limit)]
        if form.get("hiring_only") and source != "hn":
            args.append("--hiring-only")
        return f"Seed {source or 'yc'}", [args]

    if action == "discover":
        args = ["discover"] + (["--limit", str(limit)] if limit else [])
        if source:
            args += ["--source", source]
        if form.get("redo"):
            args.append("--redo")
        return "Discover", [args]

    if action == "crawl":
        args = ["crawl", "--min-score", str(min_score)] + (["--limit", str(limit)] if limit else [])
        if source:
            args += ["--source", source]
        if form.get("status") in CRAWL_STATUSES:
            args += ["--status", form["status"]]
        return "Crawl", [args]

    if action == "refresh":
        n = str(limit or 50)
        return f"Full refresh (top {n})", [
            ["seed", "--source", "yc"],
            ["seed", "--source", "hn"],
            ["discover", "--limit", n],
            ["crawl", "--min-score", str(min_score), "--limit", n],
        ]

    raise HTTPException(400, f"Unknown action {action!r}")


@app.post("/admin/run", dependencies=[Depends(require_admin)])
async def admin_run(request: Request, action: str = Form(...)):
    form = dict(await request.form())
    title, commands = build_commands(action, form)
    try:
        runner.start(title, commands)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/stop", dependencies=[Depends(require_admin)])
async def admin_stop():
    await runner.stop()
    return RedirectResponse("/admin", status_code=303)
