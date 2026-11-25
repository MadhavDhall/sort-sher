"""The website, tested with FastAPI's TestClient against a small temporary database."""

import pytest
from fastapi.testclient import TestClient

from sort_sherr import config
from sort_sherr.models import Company, JobDraft
from sort_sherr.storage import db
from sort_sherr.web import app as web
from sort_sherr.web.runner import runner


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", None)
    conn = db.connect(path)
    acme = db.upsert_company(conn, Company(
        name="Acme AI", domain="acme.ai", sources=["yc"], yc_batch="Summer 2025",
        stage="Growth", team_size=20, is_hiring=True, description="Robots for farms",
    ))
    db.save_jobs(conn, acme, 1, [
        JobDraft(title="Software Engineering Intern", location="Bangalore", is_internship=True,
                 remote=True, apply_url="https://jobs.ashbyhq.com/acme/1"),
        JobDraft(title="Founding Engineer", location="San Francisco", is_internship=False,
                 apply_url="https://jobs.ashbyhq.com/acme/2"),
    ])
    db.upsert_company(conn, Company(name="Quiet Startup", domain="quiet.dev", sources=["hn"]))
    conn.close()
    return TestClient(web.app)


def test_jobs_page_lists_jobs(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Software Engineering Intern" in r.text and "Founding Engineer" in r.text
    assert "2 active jobs match" in r.text


def test_jobs_filters(client):
    r = client.get("/", params={"internship": "1"})
    assert "Software Engineering Intern" in r.text and "Founding Engineer" not in r.text
    r = client.get("/", params={"q": "founding"})
    assert "Founding Engineer" in r.text and "Software Engineering Intern" not in r.text


def test_remote_filter(client):
    r = client.get("/", params={"remote": "remote"})
    assert "Software Engineering Intern" in r.text and "Founding Engineer" not in r.text
    r = client.get("/", params={"remote": "onsite"})  # nothing is marked on-site in the test data
    assert "Software Engineering Intern" not in r.text


def test_bad_filter_values_are_ignored(client):
    r = client.get("/", params={"min_score": "abc", "tier": "99", "page": "-5", "sort": "drop table"})
    assert r.status_code == 200  # no crash, invalid values fall back to defaults


def test_companies_page_keeps_companies_without_jobs(client):
    r = client.get("/companies")
    assert "Acme AI" in r.text and "Quiet Startup" in r.text
    r = client.get("/companies", params={"has_jobs": "1"})
    assert "Acme AI" in r.text and "Quiet Startup" not in r.text


def test_company_page_shows_score_reasons(client):
    conn = db.connect(config.DB_PATH)
    acme_id = db.find_company(conn, "acme.ai", "Acme AI").id
    r = client.get(f"/companies/{acme_id}")
    assert r.status_code == 200
    assert "Why this score?" in r.text and "Growth stage" in r.text
    assert client.get("/companies/999999").status_code == 404


def test_admin_page_and_task_endpoint(client):
    r = client.get("/admin")
    assert r.status_code == 200 and "Get new data" in r.text
    assert client.get("/admin/task").json()["busy"] in (False, True)


def test_admin_password(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3cret")
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("me", "wrong")).status_code == 401
    assert client.get("/admin", auth=("me", "s3cret")).status_code == 200
    assert client.get("/").status_code == 200  # public pages stay public


def test_admin_run_builds_safe_commands(client, monkeypatch):
    started = []
    monkeypatch.setattr(runner, "start", lambda title, commands: started.append((title, commands)))

    r = client.post("/admin/run", data={"action": "crawl", "limit": "20", "min_score": "50",
                                       "source": "hn; rm -rf /", "status": "tier3_skipped"},
                    follow_redirects=False)
    assert r.status_code == 303
    title, commands = started[-1]
    # the weird "source" value was dropped, numbers were checked, status is from the allowed list
    assert commands == [["crawl", "--min-score", "50", "--limit", "20", "--status", "tier3_skipped"]]

    client.post("/admin/run", data={"action": "refresh", "limit": "10"}, follow_redirects=False)
    assert [c[0] for c in started[-1][1]] == ["seed", "seed", "discover", "crawl"]

    assert client.post("/admin/run", data={"action": "delete_everything"}).status_code == 400
