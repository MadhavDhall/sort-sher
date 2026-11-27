"""Part 4 on the website: outreach pages. Temporary DB; run_now and the runner are faked, so no subprocess,
no LLM: we check the pages and the exact CLI arguments they would run (every value validated)."""

import pytest

from sort_sherr import config
from sort_sherr.storage import db
from sort_sherr.tracker import lifecycle
from sort_sherr.web import outreach_pages
from test_draft_web import drafted, quick  # noqa: F401  (pytest fixtures)
from test_match_web import setup, started  # noqa: F401


@pytest.fixture
def fast(monkeypatch):
    """Records run_now() calls made by the outreach pages."""
    calls = []

    async def fake(args, timeout=60):
        calls.append(args)
        return 0, "ok"

    monkeypatch.setattr(outreach_pages, "run_now", fake)
    return calls


@pytest.fixture
def sent(drafted):  # noqa: F811
    """Variant A approved and sent on 2 Nov 2026."""
    conn = db.connect(config.DB_PATH)
    db.approve_draft(conn, drafted["draft_ids"][0], "Hi team,\n\nfinal", "PaperPal and agents")
    oid, _ = lifecycle.log_sent(conn, drafted["draft_ids"][0], at="2026-11-02")
    return {**drafted, "oid": oid, "conn": conn}


def test_drafts_page_i_sent_it(drafted, fast):  # noqa: F811
    c, cid, co = drafted["client"], drafted["cid"], drafted["company"]
    conn = db.connect(config.DB_PATH)
    assert c.post(f"/match/candidates/{cid}/drafts/{co}/sent", data={"channel": "email"}).status_code == 400  # not approved
    db.approve_draft(conn, drafted["draft_ids"][1], "Hi team,\n\nfinal", "PaperPal and agents")
    page = c.get(f"/match/candidates/{cid}/drafts/{co}")
    assert "I sent it" in page.text
    r = c.post(f"/match/candidates/{cid}/drafts/{co}/sent", data={"channel": "linkedin", "at": "2026-11-20"},
               follow_redirects=False)
    assert r.status_code == 303
    assert fast[-1] == ["sent", str(drafted["draft_ids"][1]), "--channel", "linkedin", "--at=2026-11-20"]
    for bad in ({"channel": "carrier pigeon"}, {"channel": "email", "at": "; rm -rf /"}):
        assert c.post(f"/match/candidates/{cid}/drafts/{co}/sent", data=bad).status_code == 400


def test_home_today_and_table(sent):
    c, cid = sent["client"], sent["cid"]
    r = c.get(f"/match/candidates/{cid}/outreach")
    assert r.status_code == 200
    assert "Acme AI" in r.text and "follow-up 1" in r.text and "late" in r.text  # due since 9 Nov
    assert "Reply rate" in r.text


def test_one_outreach_page_and_forms(sent, fast):
    c, cid, oid = sent["client"], sent["cid"], sent["oid"]
    r = c.get(f"/match/candidates/{cid}/outreach/{oid}")
    assert r.status_code == 200 and "Timeline" in r.text and "Draft follow-up 1" in r.text

    c.post(f"/match/candidates/{cid}/outreach/{oid}/reply", data={"type": "positive", "note": "--wants\na call", "at": "2026-11-05"})
    assert fast[-1] == ["reply", str(oid), "--type", "positive", "--at=2026-11-05", "--note=--wants a call"]
    c.post(f"/match/candidates/{cid}/outreach/{oid}/status", data={"status": "interview", "when": "2026-12-03T15:00"})
    assert fast[-1] == ["status", str(oid), "interview", "--when=2026-12-03 15:00"]

    assert c.post(f"/match/candidates/{cid}/outreach/{oid}/reply", data={"type": "ecstatic"}).status_code == 400
    assert c.post(f"/match/candidates/{cid}/outreach/{oid}/status", data={"status": "drafted"}).status_code == 400
    assert c.post(f"/match/candidates/{cid}/outreach/{oid}/reply", data={"type": "neutral", "note": "x" * 400}).status_code == 400
    assert c.get(f"/match/candidates/{cid + 1}/outreach/{oid}").status_code == 404  # another candidate's id


def test_followup_task_and_sent(sent, fast, started):  # noqa: F811
    c, cid, oid, conn = sent["client"], sent["cid"], sent["oid"], sent["conn"]
    r = c.post(f"/match/candidates/{cid}/outreach/{oid}/followup", data={"value": "shipped PDF export"}, follow_redirects=False)
    assert r.status_code == 303 and "/match/task" in r.headers["location"]
    assert started[-1] == ("Draft follow-up", [["followup", str(oid), "--no-review", "--value=shipped PDF export"]])

    fu = db.save_draft(conn, {"match_id": None, "candidate_id": cid, "company_id": sent["company"], "variant": "A",
                              "style": "follow-up", "subject": "Re: PaperPal and agents", "body": "Hi team,\n\nnew thing",
                              "facts_used": [], "student_items_used": [], "check_status": "ok", "check_notes": [],
                              "lint_flags": [], "outreach_id": oid, "followup_number": 1})
    assert "Follow-up 1" in c.get(f"/match/candidates/{cid}/outreach/{oid}").text
    c.post(f"/match/candidates/{cid}/outreach/{oid}/approve", data={"draft_id": str(fu), "subject": "Re: x", "body": "Hi\r\n\r\nok"})
    assert fast[-1] == ["approve-draft", "--draft", str(fu), "--subject=Re: x", "--body=Hi\n\nok"]
    c.post(f"/match/candidates/{cid}/outreach/{oid}/sent", data={"draft_id": str(fu), "at": "2026-11-10"})
    assert fast[-1] == ["sent", str(fu), "--at=2026-11-10"]
    first = sent["draft_ids"][0]  # the first email isn't a follow-up of this outreach
    assert c.post(f"/match/candidates/{cid}/outreach/{oid}/sent", data={"draft_id": str(first)}).status_code == 400


def test_stats_report_and_csv(sent):
    c, cid, oid, conn = sent["client"], sent["cid"], sent["oid"], sent["conn"]
    lifecycle.log_reply(conn, oid, "positive", at="2026-11-05")
    r = c.get(f"/match/candidates/{cid}/outreach/stats")
    assert r.status_code == 200 and "100%" in r.text and "n&lt;10" in r.text and "Too few to judge" in r.text
    rep = c.get(f"/match/candidates/{cid}/outreach/report.html")
    assert rep.status_code == 200 and "attachment" in rep.headers["content-disposition"] and "Funnel" in rep.text
    csv = c.get(f"/match/candidates/{cid}/outreach/export.csv")
    assert csv.text.splitlines()[0].startswith("outreach_id,company") and "Acme AI" in csv.text


def test_match_results_show_outreach_badge_and_feedback(sent):
    c, cid = sent["client"], sent["cid"]
    run_id = db.latest_run_id(sent["conn"], cid)
    r = c.get(f"/match/runs/{run_id}")
    assert r.status_code == 200 and "✉ sent" in r.text and "Your outreach so far" in r.text
