"""Hosted site: invite-only accounts, login, and the rule that matters most: a student only ever
sees their OWN resumes, shortlists, drafts, outreach and task logs (anything else is a 404)."""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from sort_sherr import cli, config
from sort_sherr.storage import db
from sort_sherr.web import app as web
from sort_sherr.web import auth, match_pages
from sort_sherr.web.runner import Task
from test_match_web import setup, started  # noqa: F401  (pytest fixtures)

PW = "correct horse battery"


@pytest.fixture
def hosted(setup, monkeypatch):  # noqa: F811
    """Login on. Users: admin, alice (owns the fixture's resume + shortlist) and bob."""
    monkeypatch.setattr(config, "LOGIN_REQUIRED", True)
    auth.failures.clear()
    conn = db.connect(config.DB_PATH)
    ids = {name: db.create_user(conn, name, auth.hash_password(PW), is_admin=(name == "admin"))
           for name in ("admin", "alice", "bob")}
    conn.execute("UPDATE candidate_profiles SET user_id = ? WHERE id = ?", (ids["alice"], setup["cid"]))
    conn.commit()

    def client(name=None):
        c = TestClient(web.app)
        if name:
            r = c.post("/login", data={"username": name, "password": PW, "next": "/"}, follow_redirects=False)
            assert r.status_code == 303, r.text
        return c

    return {**setup, "conn": conn, "ids": ids, "login": client}


def test_everything_needs_a_login(hosted):
    c = hosted["login"]()
    r = c.get(f"/match/candidates/{hosted['cid']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/login?next=/match/candidates/{hosted['cid']}"
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.get("/match/task.json").status_code == 401
    assert c.post("/match/upload").status_code == 401
    assert c.get("/healthz").json() == {"ok": True} and c.get("/login").status_code == 200
    assert c.get("/static/style.css").status_code == 200


def test_wrong_password_and_lockout(hosted):
    c = hosted["login"]()
    for _ in range(auth.MAX_FAILURES):
        assert "Wrong username or password" in c.post("/login", data={"username": "bob", "password": "nope"}).text
    r = c.post("/login", data={"username": "bob", "password": PW})  # right password, but locked for now
    assert "Too many wrong passwords" in r.text


def test_students_only_see_their_own_data(hosted):
    cid, run_id = hosted["cid"], hosted["run_id"]
    alice, bob = hosted["login"]("alice"), hosted["login"]("bob")
    pages = [f"/match/candidates/{cid}", f"/match/runs/{run_id}", f"/match/runs/{run_id}/shortlist.csv",
             f"/match/candidates/{cid}/drafts", f"/match/candidates/{cid}/outreach",
             f"/match/candidates/{cid}/outreach/stats", f"/match/candidates/{cid}/outreach/export.csv"]
    for url in pages:
        assert alice.get(url).status_code == 200, url
        assert bob.get(url).status_code == 404, url  # not 403: don't even confirm it exists
    for url in (f"/match/candidates/{cid}/run", f"/match/candidates/{cid}/forget", f"/match/candidates/{cid}/drafts/run"):
        assert bob.post(url).status_code == 404, url
    assert "Priya Raman" in alice.get("/match").text and "Priya Raman" not in bob.get("/match").text
    admin = hosted["login"]("admin")
    assert admin.get(f"/match/candidates/{cid}").status_code == 200 and "Priya Raman" in admin.get("/match").text


def test_task_log_is_private(hosted):
    alice, bob = hosted["login"]("alice"), hosted["login"]("bob")
    match_pages.runner.current = Task(99, "Parse resume", [["parse-resume"]], status="running",
                                      owner=hosted["ids"]["alice"], log=["Name  Priya Raman"])
    try:
        assert alice.get("/match/task.json").json()["task"]["log"] == ["Name  Priya Raman"]
        assert bob.get("/match/task.json").json()["task"] is None
        assert "Priya" not in bob.get("/match/task").text
    finally:
        match_pages.runner.current = None


def test_admin_only_pages(hosted):
    bob = hosted["login"]("bob")
    assert bob.get("/admin").status_code == 403 and bob.post("/admin/invite").status_code == 403
    assert "Admin</a>" not in bob.get("/match").text  # not even in the menu
    admin = hosted["login"]("admin")
    r = admin.get("/admin")
    assert r.status_code == 200 and "Invite a student" in r.text and "alice" in r.text


def test_invite_join_and_reset(hosted):
    admin = hosted["login"]("admin")
    r = admin.post("/admin/invite")
    link = r.text.split('<code id="new-link">')[1].split("</code>")[0]
    path = link.split("testserver")[1]
    new = TestClient(web.app)
    assert new.get(path).status_code == 200
    bad = new.post(path, data={"username": "x", "password": "short", "again": "short"})
    assert "at least 10" in bad.text
    assert "taken" in new.post(path, data={"username": "BOB", "password": PW, "again": PW}).text
    r = new.post(path, data={"username": "carol", "password": PW, "again": PW}, follow_redirects=False)
    assert r.status_code == 303 and new.get("/match").status_code == 200  # logged in straight away
    assert TestClient(web.app).get(path).status_code == 404  # one-time

    r = admin.post("/admin/invite", data={"reset_user": str(hosted["ids"]["bob"])})
    path = r.text.split('<code id="new-link">')[1].split("</code>")[0].split("testserver")[1]
    assert "New password" in TestClient(web.app).get(path).text
    TestClient(web.app).post(path, data={"password": "brand new password", "again": "brand new password"})
    c = TestClient(web.app)
    assert c.post("/login", data={"username": "bob", "password": "brand new password"}, follow_redirects=False).status_code == 303
    conn = hosted["conn"]
    assert conn.execute("SELECT count(*) FROM invites WHERE code_hash LIKE ?", (path.split("/")[-1] + "%",)).fetchone()[0] == 0  # only hashes stored


def test_daily_match_limit_and_resume_limit(hosted, started, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "MAX_MATCH_RUNS_PER_DAY", 1)  # the fixture's run was made today
    alice = hosted["login"]("alice")
    r = alice.post(f"/match/candidates/{hosted['cid']}/run", data={"shortlist": "50"})
    assert r.status_code == 429 and started == []
    monkeypatch.setattr(config, "MAX_RESUMES_PER_USER", 1)
    from pathlib import Path

    pdf = (Path(__file__).parent / "fixtures" / "resume_fake.pdf").read_bytes()
    assert "Delete one first" in alice.post("/match/upload", files={"resume": ("cv.pdf", pdf, "application/pdf")}).text


def test_parse_runs_for_the_logged_in_student(hosted, started):  # noqa: F811
    from pathlib import Path

    bob = hosted["login"]("bob")
    pdf = (Path(__file__).parent / "fixtures" / "resume_fake.pdf").read_bytes()
    bob.post("/match/upload", files={"resume": ("cv.pdf", pdf, "application/pdf")})
    file_id = next((hosted["tmp"] / "uploads").iterdir()).name
    bob.post("/match/parse", data={"file_id": file_id, "name": "Priya Raman", "grad_year": "2028"})
    assert started[-1][1][0][-2:] == ["--user", str(hosted["ids"]["bob"])]


def test_student_cant_forget_shared_contacts(hosted):
    conn, cid = hosted["conn"], hosted["cid"]
    db.save_draft(conn, {"match_id": None, "candidate_id": cid, "company_id": 1, "variant": "A", "style": "x",
                         "subject": "s", "body": "b", "facts_used": [], "student_items_used": [],
                         "check_status": "ok", "check_notes": [], "lint_flags": []})
    db.save_contacts(conn, 1, [{"type": "email", "value": "a@acme.ai", "source_type": "first_party",
                                "source_url": "https://acme.ai", "confidence": "high"}])
    contact = db.company_contacts(conn, 1)[0]["id"]
    r = hosted["login"]("alice").post(f"/match/candidates/{cid}/drafts/1/forget", data={"contact_id": str(contact)})
    assert r.status_code == 403


def test_passwords_and_cli(hosted):
    stored = auth.hash_password("s3cret password")
    assert stored.startswith("scrypt$") and "s3cret" not in stored
    assert auth.check_password("s3cret password", stored) and not auth.check_password("other", stored)
    assert not auth.check_password("x", "garbage")
    r = CliRunner().invoke(cli.app, ["create-user", "--username", "dave", "--admin"], input=f"{PW}\n{PW}\n")
    assert r.exit_code == 0, r.output
    assert db.get_user_by_name(hosted["conn"], "dave")["is_admin"] == 1
    r = CliRunner().invoke(cli.app, ["invite", "--base-url", "https://x.onrender.com/"])
    assert r.output.startswith("https://x.onrender.com/join/")
    code = r.output.strip().rsplit("/", 1)[1]
    assert db.get_invite(hosted["conn"], auth.code_hash(code)) is not None


def test_forget_candidate_deletes_everything(hosted):
    conn, cid = hosted["conn"], hosted["cid"]
    r = CliRunner().invoke(cli.app, ["forget", "--candidate", str(cid), "--yes"])
    assert r.exit_code == 0, r.output
    assert db.get_candidate(conn, cid) is None and db.get_run(conn, hosted["run_id"]) is None
    assert db.get_company(conn, 1) is not None  # public data stays
