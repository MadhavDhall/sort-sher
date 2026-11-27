"""The Match me pages, with a temporary DB and upload folder. No LLM, no subprocess: the runner's
start() is replaced by a fake that just records the CLI arguments it would run."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sort_sherr import config
from sort_sherr.matching.models import CandidateProfile, Education, Preferences, Project
from sort_sherr.matching.roles import suggest_roles
from sort_sherr.models import Company, JobDraft
from sort_sherr.storage import db
from sort_sherr.web import app as web
from sort_sherr.web import match_pages

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(match_pages.runner, "start", lambda title, commands, owner=None: calls.append((title, commands)))
    return calls


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", None)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(match_pages, "UPLOAD_DIR", tmp_path / "uploads")
    conn = db.connect(path)
    acme = db.upsert_company(conn, Company(name="Acme AI", domain="acme.ai", sources=["yc"], yc_batch="Winter 2026",
                                           is_hiring=True, careers_url="https://acme.ai/careers"))
    db.save_jobs(conn, acme, 1, [JobDraft(title="ML Intern", is_internship=True, apply_url="https://acme.ai/jobs/1")])
    beta = db.upsert_company(conn, Company(name="Beta Tools", domain="beta.dev", sources=["yc"], is_hiring=True,
                                           website="https://beta.dev"))
    profile = CandidateProfile(
        name="Priya Raman", skills=["Python", "PyTorch", "React"], focus_areas=["GenAI", "backend"],
        education=[Education(degree="B.Tech", branch="CSE", grad_date="2028")],
        projects=[Project(title="PaperPal", tech_stack=["LangChain"])],
    )
    cid = db.save_candidate(conn, "abc", profile.model_dump_json(), "{}", "fake")
    job_id = db.company_jobs(conn, acme)[0]["id"]
    run = {"prefs_json": Preferences(shortlist=1, interests="agents").model_dump_json(), "filter_stats": json.dumps({"kept": 2, "kept_unknown": 0}),
           "embed_model": "e", "llm_model": "m", "reranked": 1, "llm_calls": 1, "prompt_tokens": 10, "output_tokens": 5, "cost_usd": 0.0}

    def row(company, score, **extra):
        return {"company_id": company, "job_id": None, "found_by": "resume", "retrieval_score": 0.7, "rerank_score": 80,
                "final_score": score, "sub_scores": json.dumps({"stack_overlap": 15}), "why_fit": None, "why_this_role": None,
                "pitch_role": None, "reasons": "[]", "red_flags": "[]", **extra}

    run_id = db.save_match_run(conn, cid, run, [
        row(acme, 90, job_id=job_id, why_fit="You built PaperPal; they build agents.", why_this_role="ML fits you",
            reasons=json.dumps([{"resume_item": "PaperPal", "company_fact": "agents platform"}])),
        row(beta, 70, pitch_role="Backend engineering intern", red_flags=json.dumps(["team of 2"])),
    ])
    conn.close()
    return {"client": TestClient(web.app), "cid": cid, "run_id": run_id, "tmp": tmp_path}


def test_home_lists_candidates(setup):
    r = setup["client"].get("/match")
    assert r.status_code == 200 and "Priya Raman" in r.text and "Upload" in r.text and "Match me" in r.text


def test_upload_rejects_wrong_files(setup):
    c = setup["client"]
    r = c.post("/match/upload", files={"resume": ("cv.txt", b"hello", "text/plain")})
    assert "Please upload a .pdf or .docx" in r.text
    r = c.post("/match/upload", files={"resume": ("cv.pdf", b"not really a pdf", "application/pdf")})
    assert "look like a real .pdf" in r.text


def test_upload_saves_by_hash_and_asks_to_confirm_name(setup):
    data = (FIXTURES / "resume_fake.pdf").read_bytes()
    r = setup["client"].post("/match/upload", files={"resume": ("../../etc/My CV.pdf", data, "application/pdf")})
    assert r.status_code == 200
    assert 'value="Priya Raman"' in r.text  # the guess, pre-filled for the student to confirm
    saved = list((setup["tmp"] / "uploads").iterdir())
    assert len(saved) == 1 and match_pages.FILE_ID.match(saved[0].name)  # stored under a hash, not "My CV"


def test_parse_validates_then_runs_cli(setup, started):
    c = setup["client"]
    data = (FIXTURES / "resume_fake.pdf").read_bytes()
    c.post("/match/upload", files={"resume": ("cv.pdf", data, "application/pdf")})
    file_id = next((setup["tmp"] / "uploads").iterdir()).name

    assert c.post("/match/parse", data={"file_id": "../x.pdf", "name": "Priya Raman", "grad_year": "2028"}).status_code == 400
    assert c.post("/match/parse", data={"file_id": file_id, "name": "rm -rf /", "grad_year": "2028"}).status_code == 400
    assert c.post("/match/parse", data={"file_id": file_id, "name": "Priya Raman", "grad_year": "20x8"}).status_code == 400
    assert started == []

    r = c.post("/match/parse", data={"file_id": file_id, "name": " Priya  Raman ", "grad_year": "2028"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/match/task?next=/match/resume/{file_id}"
    assert started == [("Parse resume", [["parse-resume", f"uploads/{file_id}", "--name=Priya Raman", "--grad-year", "2028",
                                         "--delete-file"]])]  # the resume file is never kept


def test_candidate_page_suggests_roles_and_shortlist_sizes(setup):
    r = setup["client"].get(f"/match/candidates/{setup['cid']}")
    assert r.status_code == 200
    assert 'value="ai-ml" checked' in r.text and 'value="backend" checked' in r.text
    assert 'value="frontend" checked' in r.text  # from the React skill
    assert 'value="mobile" checked' not in r.text
    assert '<option value="100"' in r.text and "selected>50 companies" in r.text


def test_match_form_builds_safe_arguments(setup, started):
    c = setup["client"]
    form = {
        "shortlist": "100", "role": ["ai-ml", "hacker"], "remote_only": "1", "locations": "India, Remote, ;rm -rf",
        "window": "Summer 2027", "interests": "--remote-only\nLLM agents\x00", "paid_only": "1",
    }
    r = c.post(f"/match/candidates/{setup['cid']}/run", data=form, follow_redirects=False)
    assert r.status_code == 303
    args = started[0][1][0]
    assert args == ["match", "--candidate", str(setup["cid"]), "--top", "100", "--role", "ai-ml", "--remote-only",
                    "--location=India", "--location=Remote", "--paid-only", "--window=Summer 2027",
                    "--interests=--remote-only\nLLM agents"]

    started.clear()
    c.post(f"/match/candidates/{setup['cid']}/run", data={"shortlist": "9999", "quick": "1"})
    assert started[0][1][0] == ["match", "--candidate", str(setup["cid"]), "--top", "50", "--no-rerank"]


def test_run_page_shows_reasons_roles_and_csv(setup):
    c, run_id = setup["client"], setup["run_id"]
    r = c.get(f"/match/runs/{run_id}")
    assert r.status_code == 200
    assert "Your top 1 startups" in r.text  # the run's own shortlist size
    assert "You built PaperPal" in r.text and "ML Intern" in r.text and "Apply" in r.text
    assert "Beta Tools" not in r.text

    r = c.get(f"/match/runs/{run_id}", params={"n": 5})
    assert "Beta Tools" in r.text and "Ask for" in r.text and "Backend engineering intern" in r.text and "team of 2" in r.text

    csv_text = c.get(f"/match/runs/{run_id}/shortlist.csv").text
    assert csv_text.splitlines()[0].startswith("rank,company,score,role")
    assert "Acme AI" in csv_text and "https://acme.ai/jobs/1" in csv_text


def test_task_page_only_redirects_to_our_pages(setup):
    r = setup["client"].get("/match/task", params={"next": "https://evil.example"})
    assert "evil" not in r.text


def test_match_pages_are_open_but_admin_stays_locked(setup, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "secret")
    c = setup["client"]
    assert c.get("/match").status_code == 200
    assert c.get(f"/match/runs/{setup['run_id']}/shortlist.csv").status_code == 200
    assert c.get("/match/task.json").status_code == 200
    assert c.get("/admin").status_code == 401  # admin still needs the password


def test_match_task_endpoints_never_show_or_stop_admin_tasks(setup, monkeypatch):
    from sort_sherr.web.runner import Task

    stopped = []

    async def fake_stop():
        stopped.append(True)

    monkeypatch.setattr(match_pages.runner, "stop", fake_stop)
    c = setup["client"]

    monkeypatch.setattr(match_pages.runner, "current", Task(1, "Crawl", [["crawl"]], status="running", log=["secret log"]))
    assert c.get("/match/task.json").json() == {"busy": True, "task": None}
    c.post("/match/stop")
    assert stopped == []
    assert "busy with an admin task" in c.get("/match/task").text

    monkeypatch.setattr(match_pages.runner, "current", Task(2, "Match", [["match"]], status="running", log=["ranking"]))
    assert c.get("/match/task.json").json()["task"]["log"] == ["ranking"]
    c.post("/match/stop")
    assert stopped == [True]


def test_suggest_roles():
    p = CandidateProfile(focus_areas=["GenAI", "backend"], skills=["React"])
    assert [r for r, _ in suggest_roles(p)] == ["ai-ml", "backend", "sde", "frontend"]
