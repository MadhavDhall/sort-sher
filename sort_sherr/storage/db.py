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

-- ----- Part 2: matching -----

-- pii_json (name, email, phone, links) is personal data: it stays in this local file only.
CREATE TABLE IF NOT EXISTS candidate_profiles (
    id INTEGER PRIMARY KEY,
    resume_hash TEXT NOT NULL UNIQUE,
    profile_json TEXT NOT NULL,
    pii_json TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL
);

-- One row per `match` run, so runs can be compared later.
CREATE TABLE IF NOT EXISTS match_runs (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidate_profiles(id),
    prefs_json TEXT NOT NULL,
    filter_stats TEXT NOT NULL DEFAULT '{}',
    embed_model TEXT,
    llm_model TEXT,
    reranked INTEGER NOT NULL DEFAULT 0,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Every company in a run's retrieval pool (not just the top 15), so eval can compare
-- "retrieval only" vs "retrieval + rerank" orderings of the same run.
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES match_runs(id),
    candidate_id INTEGER NOT NULL REFERENCES candidate_profiles(id),
    company_id INTEGER NOT NULL REFERENCES companies(id),
    job_id INTEGER REFERENCES jobs(id),
    found_by TEXT,
    retrieval_score REAL,
    rerank_score INTEGER,
    final_score REAL,
    sub_scores TEXT NOT NULL DEFAULT '{}',
    why_fit TEXT,
    why_this_role TEXT,
    pitch_role TEXT,
    reasons TEXT NOT NULL DEFAULT '[]',
    red_flags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

-- Embedding vectors (float32 bytes), keyed by a hash of the text: re-runs only embed what changed.
CREATE TABLE IF NOT EXISTS embeddings (
    content_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (content_hash, model)
);

-- LLM rerank answers, keyed by hash(profile + preferences + company document + model).
CREATE TABLE IF NOT EXISTS rerank_cache (
    cache_key TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
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
    add_missing_columns(conn)
    return conn


# Columns added after a table was first created. CREATE TABLE IF NOT EXISTS doesn't change an
# existing table, so older databases get them here. (PRAGMA table_info is SQLite's way to list
# columns; on Postgres this becomes ALTER TABLE ... ADD COLUMN IF NOT EXISTS.)
NEW_COLUMNS = [("matches", "pitch_role", "TEXT")]


def add_missing_columns(conn) -> None:
    for table, column, sql_type in NEW_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    conn.commit()


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


# ---------- Part 2: candidates ----------


def save_candidate(conn, resume_hash: str, profile_json: str, pii_json: str, model: str) -> int:
    cur = conn.execute(
        "INSERT INTO candidate_profiles (resume_hash, profile_json, pii_json, model, created_at) VALUES (?, ?, ?, ?, ?)",
        (resume_hash, profile_json, pii_json, model, now()),
    )
    conn.commit()
    return cur.lastrowid


def get_candidate_by_hash(conn, resume_hash: str) -> dict | None:
    row = conn.execute("SELECT * FROM candidate_profiles WHERE resume_hash = ?", (resume_hash,)).fetchone()
    return dict(row) if row else None


def update_candidate_profile(conn, candidate_id: int, profile_json: str) -> None:
    """Student corrections (e.g. graduation year) after parsing."""
    conn.execute("UPDATE candidate_profiles SET profile_json = ? WHERE id = ?", (profile_json, candidate_id))
    conn.commit()


def get_candidate(conn, candidate_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM candidate_profiles WHERE id = ?", (candidate_id,)).fetchone()
    return dict(row) if row else None


# ---------- Part 2: what matching reads ----------


def matching_companies(conn) -> list[dict]:
    """Every company, with its active jobs attached (list of dicts) for building match documents."""
    companies = []
    for row in conn.execute("SELECT * FROM companies ORDER BY id"):
        c = dict(row)
        for f in LIST_FIELDS:
            c[f] = json.loads(c[f] or "[]")
        c["jobs"] = []
        companies.append(c)
    by_id = {c["id"]: c for c in companies}
    for job in conn.execute(
        "SELECT id, company_id, title, team, location, remote, is_internship, employment_type, requirements, "
        "description, apply_url FROM jobs WHERE is_active = 1 ORDER BY company_id, is_internship DESC, id"
    ):
        if job["company_id"] in by_id:
            by_id[job["company_id"]]["jobs"].append(dict(job))
    return companies


# ---------- Part 2: embedding + rerank caches ----------


def get_embeddings(conn, hashes: list[str], model: str) -> dict[str, bytes]:
    found = {}
    for i in range(0, len(hashes), 500):  # chunks: databases limit the number of ? in one query
        chunk = hashes[i : i + 500]
        marks = ", ".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT content_hash, vector FROM embeddings WHERE model = ? AND content_hash IN ({marks})",
            [model, *chunk],
        ):
            found[r["content_hash"]] = r["vector"]
    return found


def put_embeddings(conn, rows: list[tuple[str, bytes]], model: str) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO embeddings (content_hash, model, vector) VALUES (?, ?, ?)",
        [(h, model, v) for h, v in rows],
    )
    conn.commit()


def rerank_cache_get(conn, key: str) -> str | None:
    row = conn.execute("SELECT result_json FROM rerank_cache WHERE cache_key = ?", (key,)).fetchone()
    return row["result_json"] if row else None


def rerank_cache_put(conn, key: str, result_json: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO rerank_cache (cache_key, result_json, created_at) VALUES (?, ?, ?)",
        (key, result_json, now()),
    )
    conn.commit()


# ---------- Part 2: match runs ----------


def save_match_run(conn, candidate_id: int, run: dict, matches: list[dict]) -> int:
    """run: prefs_json, filter_stats, embed_model, llm_model, reranked, llm_calls, prompt_tokens,
    output_tokens, cost_usd. matches: one dict per company in the pool."""
    ts = now()
    cols = ["candidate_id", *run, "created_at"]
    cur = conn.execute(
        f"INSERT INTO match_runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [candidate_id, *run.values(), ts],
    )
    run_id = cur.lastrowid
    for m in matches:
        mcols = ["run_id", "candidate_id", *m, "created_at"]
        conn.execute(
            f"INSERT INTO matches ({', '.join(mcols)}) VALUES ({', '.join('?' for _ in mcols)})",
            [run_id, candidate_id, *m.values(), ts],
        )
    conn.commit()
    return run_id


def latest_run_id(conn, candidate_id: int, reranked_only: bool = False) -> int | None:
    sql = "SELECT max(id) FROM match_runs WHERE candidate_id = ?" + (" AND reranked = 1" if reranked_only else "")
    row = conn.execute(sql, (candidate_id,)).fetchone()
    return row[0] if row else None


def get_run(conn, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM match_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def run_matches(conn, run_id: int, order: str = "final") -> list[dict]:
    """All matches of a run, joined with company name + best job title.
    order: "final" (reranked ranking) or "retrieval" (embedding similarity only)."""
    order_sql = {
        "final": "m.final_score DESC, m.retrieval_score DESC",
        "retrieval": "m.retrieval_score DESC",
    }[order]
    rows = conn.execute(
        "SELECT m.*, c.name AS company_name, c.website, c.yc_batch, c.careers_url, c.description, c.team_size, "
        "c.contact, j.title AS job_title, j.apply_url, j.is_internship AS job_is_internship "
        "FROM matches m JOIN companies c ON c.id = m.company_id LEFT JOIN jobs j ON j.id = m.job_id "
        f"WHERE m.run_id = ? ORDER BY {order_sql}",
        (run_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for f in ("sub_scores", "reasons", "red_flags"):
            d[f] = json.loads(d[f])
        out.append(d)
    return out


def list_candidates(conn) -> list[dict]:
    """Parsed resumes, newest first (profile only; pii_json is not needed for listing)."""
    rows = conn.execute(
        "SELECT p.id, p.profile_json, p.created_at, count(r.id) AS runs FROM candidate_profiles p "
        "LEFT JOIN match_runs r ON r.candidate_id = p.id GROUP BY p.id, p.profile_json, p.created_at ORDER BY p.id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def candidate_runs(conn, candidate_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT r.*, count(m.id) AS pool FROM match_runs r LEFT JOIN matches m ON m.run_id = r.id "
        "WHERE r.candidate_id = ? GROUP BY r.id ORDER BY r.id DESC",
        (candidate_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_run_for_company(conn, candidate_id: int, company_id: int) -> int | None:
    row = conn.execute(
        "SELECT max(run_id) FROM matches WHERE candidate_id = ? AND company_id = ?", (candidate_id, company_id)
    ).fetchone()
    return row[0] if row else None


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
