"""Storage rules: dedupe across runs, vanished jobs become inactive, companies merge across sources."""

from sort_sherr.models import Company, JobDraft
from sort_sherr.storage import db


def test_dedupe_and_mark_inactive():
    conn = db.connect(":memory:")
    cid = db.upsert_company(conn, Company(name="Acme", domain="acme.com", sources=["yc"]))
    a = JobDraft(title="Engineer", apply_url="https://jobs.acme.com/1")
    b = JobDraft(title="Designer", apply_url="https://jobs.acme.com/2")

    assert db.save_jobs(conn, cid, 1, [a, b]) == {"new": 2, "updated": 0, "deactivated": 0}
    # Second run: "a" still there, "b" gone.
    assert db.save_jobs(conn, cid, 1, [a]) == {"new": 0, "updated": 1, "deactivated": 1}

    rows = {r["title"]: r["is_active"] for r in conn.execute("SELECT title, is_active FROM jobs")}
    assert rows == {"Engineer": 1, "Designer": 0}


def test_company_kept_with_zero_jobs():
    conn = db.connect(":memory:")
    cid = db.upsert_company(conn, Company(name="Tiny", domain="tiny.dev", sources=["yc"], is_hiring=True))
    db.save_jobs(conn, cid, 1, [])
    assert db.list_companies(conn)[0].name == "Tiny"


def test_merge_sources_without_erasing_fields():
    conn = db.connect(":memory:")
    db.upsert_company(conn, Company(name="Acme", domain="acme.com", sources=["yc"], yc_batch="Winter 2025", top_company=True))
    db.upsert_company(conn, Company(name="ACME Inc", domain="acme.com", sources=["hn"], contact="jobs@acme.com"))

    [c] = db.list_companies(conn)
    assert c.sources == ["hn", "yc"]
    assert c.yc_batch == "Winter 2025"  # not erased by the HN record
    assert c.top_company is True
    assert c.contact == "jobs@acme.com"
    assert c.name == "Acme"  # a different source only fills empty fields, it doesn't rename


def test_same_name_different_domain_is_not_merged():
    conn = db.connect(":memory:")
    db.upsert_company(conn, Company(name="Shepherd", domain="shepherd.ai", sources=["yc"]))
    db.upsert_company(conn, Company(name="Shepherd", domain="shepherdinsurance.com", sources=["hn"]))
    assert len(db.list_companies(conn)) == 2


def test_hn_posts_survive_a_crawl():
    conn = db.connect(":memory:")
    cid = db.upsert_company(conn, Company(name="Acme", domain="acme.com", sources=["hn"]))
    db.save_jobs(conn, cid, 0, [JobDraft(title="Engineer", apply_url="https://news.ycombinator.com/item?id=1")])
    db.save_jobs(conn, cid, 1, [JobDraft(title="Designer", apply_url="https://jobs.acme.com/2")])
    assert conn.execute("SELECT count(*) FROM jobs WHERE is_active = 1").fetchone()[0] == 2
