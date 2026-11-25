"""All database code lives here (plain sqlite3, portable SQL so Postgres is an easy swap later).

Lists (tags, locations, sources) are stored as JSON text. Datetimes as ISO text.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..models import Company, JobDraft
from ..scoring import quality_score

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    website TEXT,
    domain TEXT UNIQUE,
    sources TEXT NOT NULL DEFAULT '[]',
    yc_slug TEXT,
    yc_batch TEXT,
    team_size INTEGER,
    is_hiring INTEGER,
    stage TEXT,
    top_company INTEGER,
    locations TEXT NOT NULL DEFAULT '[]',
    description TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    contact TEXT,
    quality_score INTEGER NOT NULL DEFAULT 0,
    careers_url TEXT,
    ats_type TEXT,
    ats_slug TEXT,
    discover_status TEXT,
    crawl_status TEXT,
    crawl_error TEXT,
    last_crawled_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    dedupe_key TEXT NOT NULL,
    title TEXT NOT NULL,
    team TEXT,
    location TEXT,
    remote INTEGER,
    is_internship INTEGER,
    employment_type TEXT,
    description TEXT,
    requirements TEXT,
    apply_url TEXT,
    posted_at TEXT,
    source_tier INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (company_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    content_hash TEXT PRIMARY KEY,
    jobs_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

LIST_FIELDS = ("sources", "locations", "tags")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # timeout: wait up to 30 s if another process (e.g. a crawl started from the admin page) is writing
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")  # lets the website read while a crawl writes
    conn.executescript(SCHEMA)
    return conn


# ---------- companies ----------


def row_to_company(row: sqlite3.Row) -> Company:
    data = dict(row)
    for f in LIST_FIELDS:
        data[f] = json.loads(data[f] or "[]")
    return Company(**data)


def find_company(conn, domain: str | None, name: str) -> Company | None:
    """Same company = same domain. Only when we don't know the domain do we fall back to the name
    (two different startups can share a name, so a known domain always wins)."""
    if domain:
        row = conn.execute("SELECT * FROM companies WHERE domain = ?", (domain,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM companies WHERE lower(name) = lower(?)", (name,)).fetchone()
    return row_to_company(row) if row else None


def upsert_company(conn, new: Company) -> int:
    """Insert, or merge into the existing row. Merge rules:
    - list fields (sources, tags, locations) are combined
    - the SAME source (e.g. re-seeding YC) may update values; empty values never erase old ones
    - a DIFFERENT source (e.g. HN post for a YC company) only fills fields that are still empty
    The quality score is recomputed after merging."""
    old = find_company(conn, new.domain, new.name)
    if old:
        same_source = bool(set(old.sources) & set(new.sources))
        merged = old.model_dump()
        for key, value in new.model_dump(exclude={"id"}).items():
            if key in LIST_FIELDS:
                merged[key] = merged[key] + [v for v in value if v not in merged[key]]
            elif value is not None and (same_source or merged[key] is None):
                merged[key] = value
        merged["sources"] = sorted(merged["sources"])
        company = Company(**merged)
    else:
        company = new
    company.quality_score = quality_score(company)

    data = company.model_dump(exclude={"id"}, mode="json")
    for f in LIST_FIELDS:
        data[f] = json.dumps(data[f])
    cols = list(data)

    if old:
        sets = ", ".join(f"{c} = ?" for c in cols)
        conn.execute(f"UPDATE companies SET {sets} WHERE id = ?", [*data.values(), old.id])
        company_id = old.id
    else:
        marks = ", ".join("?" for _ in cols)
        cur = conn.execute(f"INSERT INTO companies ({', '.join(cols)}) VALUES ({marks})", list(data.values()))
        company_id = cur.lastrowid
    conn.commit()
    return company_id


def update_company(conn, company_id: int, **fields) -> None:
    """Set a few columns, e.g. update_company(conn, 3, careers_url=..., ats_type=...)."""
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE companies SET {sets} WHERE id = ?", [*fields.values(), company_id])
    conn.commit()


def list_companies(conn, where: str = "1=1", params: tuple = (), limit: int | None = None) -> list[Company]:
    """Companies ordered best-first by quality_score."""
    sql = f"SELECT * FROM companies WHERE {where} ORDER BY quality_score DESC, id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [row_to_company(r) for r in conn.execute(sql, params)]


# ---------- jobs ----------


def dedupe_key(job: JobDraft) -> str:
    """Prefer the apply URL; fall back to normalized title + location."""
    if job.apply_url:
        return "url:" + job.apply_url.strip().rstrip("/").lower()
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()  # noqa: E731
    return f"title:{norm(job.title)}|{norm(job.location)}"


def save_jobs(conn, company_id: int, tier: int, drafts: list[JobDraft]) -> dict:
    """Upsert this crawl's jobs; mark this company's jobs that disappeared as inactive.
    Returns counts: {"new": .., "updated": .., "deactivated": ..}."""
    ts = now()
    seen_keys = set()
    seen_title_location = set()
    new = updated = 0
    for job in drafts:
        key = dedupe_key(job)
        # Same title + location twice (some boards post one role twice with two links)
        # is one job for a student, so we keep only the first.
        title_location = (job.title.lower(), (job.location or "").lower())
        if key in seen_keys or title_location in seen_title_location:
            continue
        seen_keys.add(key)
        seen_title_location.add(title_location)
        data = job.model_dump(mode="json")
        existing = conn.execute(
            "SELECT id FROM jobs WHERE company_id = ? AND dedupe_key = ?", (company_id, key)
        ).fetchone()
        if existing:
            sets = ", ".join(f"{c} = ?" for c in data)
            conn.execute(
                f"UPDATE jobs SET {sets}, source_tier = ?, last_seen_at = ?, is_active = 1 WHERE id = ?",
                [*data.values(), tier, ts, existing["id"]],
            )
            updated += 1
        else:
            cols = [*data, "company_id", "dedupe_key", "source_tier", "first_seen_at", "last_seen_at"]
            values = [*data.values(), company_id, key, tier, ts, ts]
            conn.execute(
                f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", values
            )
            new += 1

    # Anything still active for this company that we did NOT see this time is gone.
    # HN posts (tier 0) and crawled jobs (tiers 1-3) are tracked separately, so a crawl
    # never hides the HN post and vice versa.
    same_kind = "source_tier = 0" if tier == 0 else "source_tier != 0"
    active = conn.execute(
        f"SELECT id, dedupe_key FROM jobs WHERE company_id = ? AND is_active = 1 AND {same_kind}", (company_id,)
    ).fetchall()
    gone = [r["id"] for r in active if r["dedupe_key"] not in seen_keys]
    for job_id in gone:
        conn.execute("UPDATE jobs SET is_active = 0 WHERE id = ?", (job_id,))
    conn.commit()
    return {"new": new, "updated": updated, "deactivated": len(gone)}


def deactivate_hn_posts(conn) -> None:
    """Called before loading a new month's HN thread: last month's posts are no longer current."""
    conn.execute("UPDATE jobs SET is_active = 0 WHERE source_tier = 0")
    conn.commit()


# ---------- LLM cache ----------


def cache_get(conn, content_hash: str) -> list[JobDraft] | None:
    row = conn.execute("SELECT jobs_json FROM llm_cache WHERE content_hash = ?", (content_hash,)).fetchone()
    if row is None:
        return None
    return [JobDraft(**j) for j in json.loads(row["jobs_json"])]


def cache_put(conn, content_hash: str, jobs: list[JobDraft]) -> None:
    payload = json.dumps([j.model_dump(mode="json") for j in jobs])
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache (content_hash, jobs_json, created_at) VALUES (?, ?, ?)",
        (content_hash, payload, now()),
    )
    conn.commit()


# ---------- queries for the web app (read-only) ----------

JOB_SORTS = {
    "score": "c.quality_score DESC, coalesce(j.posted_at, j.first_seen_at) DESC",
    "newest": "coalesce(j.posted_at, j.first_seen_at) DESC",
    "company": "lower(c.name), lower(j.title)",
}
COMPANY_SORTS = {
    "score": "c.quality_score DESC, c.name",
    "jobs": "active_jobs DESC, c.quality_score DESC",
    "interns": "internships DESC, active_jobs DESC",
    "name": "lower(c.name)",
    "batch": "c.yc_batch DESC",
}


def search_jobs(
    conn, q: str = "", internship: bool = False, remote: str = "", tier: int | None = None,
    source: str = "", min_score: int = 0, sort: str = "score", limit: int = 50, offset: int = 0,
) -> tuple[list[dict], int]:
    """Active jobs joined with their company. Returns (rows for this page, total matching)."""
    where, params = ["j.is_active = 1", "c.quality_score >= ?"], [min_score]
    if q:
        where.append("(j.title LIKE ? OR c.name LIKE ? OR j.location LIKE ? OR j.team LIKE ?)")
        params += [f"%{q}%"] * 4
    if internship:
        where.append("j.is_internship = 1")
    if remote == "remote":
        where.append("j.remote = 1")
    elif remote == "onsite":
        where.append("j.remote = 0")
    if tier is not None:
        where.append("j.source_tier = ?")
        params.append(tier)
    if source:
        where.append("c.sources LIKE ?")
        params.append(f'%"{source}"%')
    sql_where = " AND ".join(where)
    base = f"FROM jobs j JOIN companies c ON c.id = j.company_id WHERE {sql_where}"
    total = conn.execute(f"SELECT count(*) {base}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT j.*, c.name AS company_name, c.quality_score, c.yc_batch, c.sources AS company_sources, "
        f"c.careers_url AS company_careers_url "
        f"{base} ORDER BY {JOB_SORTS.get(sort, JOB_SORTS['score'])} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


def search_companies(
    conn, q: str = "", source: str = "", ats: str = "", hiring: bool = False, has_jobs: bool = False,
    has_internships: bool = False, min_score: int = 0, sort: str = "score", limit: int = 50, offset: int = 0,
) -> tuple[list[dict], int]:
    """Companies with their active job + internship counts. Returns (rows, total)."""
    counts = (
        "SELECT company_id, count(*) AS active_jobs, sum(is_internship = 1) AS internships "
        "FROM jobs WHERE is_active = 1 GROUP BY company_id"
    )
    where, params = ["c.quality_score >= ?"], [min_score]
    if q:
        where.append("(c.name LIKE ? OR c.description LIKE ? OR c.tags LIKE ?)")
        params += [f"%{q}%"] * 3
    if source:
        where.append("c.sources LIKE ?")
        params.append(f'%"{source}"%')
    if ats:
        where.append("c.ats_type = ?")
        params.append(ats)
    if hiring:
        where.append("c.is_hiring = 1")
    if has_jobs:
        where.append("coalesce(n.active_jobs, 0) > 0")
    if has_internships:
        where.append("coalesce(n.internships, 0) > 0")
    base = f"FROM companies c LEFT JOIN ({counts}) n ON n.company_id = c.id WHERE {' AND '.join(where)}"
    total = conn.execute(f"SELECT count(*) {base}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT c.*, coalesce(n.active_jobs, 0) AS active_jobs, coalesce(n.internships, 0) AS internships "
        f"{base} ORDER BY {COMPANY_SORTS.get(sort, COMPANY_SORTS['score'])} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for f in LIST_FIELDS:
            d[f] = json.loads(d[f] or "[]")
        result.append(d)
    return result, total


def get_company(conn, company_id: int) -> Company | None:
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    return row_to_company(row) if row else None


def company_jobs(conn, company_id: int) -> list[dict]:
    """All jobs of one company: active first, then by tier and title."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE company_id = ? ORDER BY is_active DESC, source_tier, lower(title)", (company_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- stats ----------


def stats(conn) -> dict:
    q = lambda sql: [tuple(r) for r in conn.execute(sql)]  # noqa: E731
    return {
        "companies": q("SELECT count(*) FROM companies")[0][0],
        "companies_by_source": q("SELECT sources, count(*) FROM companies GROUP BY sources ORDER BY 2 DESC"),
        "discover_status": q(
            "SELECT coalesce(discover_status, 'not run'), count(*) FROM companies GROUP BY 1 ORDER BY 2 DESC"
        ),
        "ats_type": q("SELECT ats_type, count(*) FROM companies WHERE ats_type IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"),
        "crawl_status": q(
            "SELECT coalesce(crawl_status, 'not run'), count(*) FROM companies GROUP BY 1 ORDER BY 2 DESC"
        ),
        "jobs_active": q("SELECT count(*) FROM jobs WHERE is_active = 1")[0][0],
        "jobs_inactive": q("SELECT count(*) FROM jobs WHERE is_active = 0")[0][0],
        "internships_active": q("SELECT count(*) FROM jobs WHERE is_active = 1 AND is_internship = 1")[0][0],
        "jobs_by_tier": q("SELECT source_tier, count(*) FROM jobs WHERE is_active = 1 GROUP BY 1 ORDER BY 1"),
        "companies_by_tier": q(
            "SELECT source_tier, count(DISTINCT company_id) FROM jobs WHERE is_active = 1 GROUP BY 1 ORDER BY 1"
        ),
        "failures": q(
            "SELECT name, coalesce(crawl_status, discover_status), coalesce(crawl_error, '') FROM companies "
            "WHERE crawl_status NOT IN ('ok', 'no_jobs') "
            "   OR (crawl_status IS NULL AND discover_status NOT IN ('ats', 'careers_page')) "
            "ORDER BY quality_score DESC LIMIT 25"
        ),
    }
