"""The email-draft pages (Part 3 on the website). Temporary DB; the runner and run_now are faked, so
no subprocess, no LLM, no network: we only check the pages and the CLI arguments they would run."""

import pytest

from sort_sherr.storage import db
from sort_sherr.web import draft_pages
from test_match_web import setup, started  # noqa: F401  (pytest fixtures)


@pytest.fixture
def quick(monkeypatch):
    """Records run_now() calls instead of starting a subprocess."""
    calls = []

    async def fake(args, timeout=60):
        calls.append(args)
        return 0, "lint: subject: generic; name their product\nApproved draft"

    monkeypatch.setattr(draft_pages, "run_now", fake)
    return calls


@pytest.fixture
def drafted(setup):  # noqa: F811
    from sort_sherr import config

    conn = db.connect(config.DB_PATH)
    acme = 1
    fact = {"id": "F1", "section": "what_they_build", "text": "Agents platform", "source_url": "https://acme.ai", "date": None}
    ids = [db.save_draft(conn, {"match_id": None, "candidate_id": setup["cid"], "company_id": acme, "variant": v,
                                "style": s, "subject": f"PaperPal and agents {v}", "body": f"Hi team,\n\nBody {v}",
                                "facts_used": [fact], "student_items_used": [], "check_status": "ok",
                                "check_notes": [], "lint_flags": []})
           for v, s in (("A", "product-led"), ("B", "project-led"))]
    db.save_contacts(conn, acme, [{"type": "email", "value": "founders@acme.ai", "source_type": "first_party",
                                   "source_url": "https://acme.ai/about", "confidence": "high", "mx_ok": 1}])
    conn.close()
    return {**setup, "company": acme, "draft_ids": ids}


def test_drafts_home_shows_form_top_matches_and_drafts(drafted):
    r = drafted["client"].get(f"/match/candidates/{drafted['cid']}/drafts")
    assert r.status_code == 200
    assert "Research &amp; draft" in r.text and "Acme AI" in r.text and "Beta Tools" in r.text
    assert "10 of 10" in r.text or "of 10" in r.text  # daily cap shown
    assert "A: ok" in r.text


def test_draft_form_builds_safe_arguments(drafted, started):  # noqa: F811
    c, cid = drafted["client"], drafted["cid"]
    form = {"top": "5", "setup": "remote", "window": "can start now (remote); onsite mid-May to mid-July 2027",
            "github": "https://github.com/priya", "redo": "1"}
    r = c.post(f"/match/candidates/{cid}/drafts/run", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/match/task?next=/match/candidates/{cid}/drafts"
    title, [args] = started[0]
    assert title == "Draft emails"
    assert args == ["draft", "--candidate", str(cid), "--top", "5",
                    "--window=can start now (remote); onsite mid-May to mid-July 2027", "--setup=remote",
                    "--github=https://github.com/priya", "--redo"]

    bad = [{**form, "window": "$(rm -rf ~)"}, {**form, "setup": "mars"}, {**form, "github": "javascript:alert(1)"},
           {**form, "github": "https://x.com/a b"}]
    for f in bad:
        assert c.post(f"/match/candidates/{cid}/drafts/run", data=f).status_code == 400
    r = c.post(f"/match/candidates/{cid}/drafts/run", data={**form, "top": "999"}, follow_redirects=False)
    assert started[-1][1][0][4] == "5"  # not one of the choices -> default


def test_company_page_shows_contact_source_and_facts(drafted):
    r = drafted["client"].get(f"/match/candidates/{drafted['cid']}/drafts/{drafted['company']}")
    assert r.status_code == 200
    assert "founders@acme.ai" in r.text and "https://acme.ai/about" in r.text  # recipient + where it was found
    assert "Variant A" in r.text and "Variant B" in r.text and "Agents platform" in r.text


def test_approve_runs_the_cli_with_edited_text(drafted, quick):
    c, cid, co = drafted["client"], drafted["cid"], drafted["company"]
    b = drafted["draft_ids"][1]
    r = c.post(f"/match/candidates/{cid}/drafts/{co}/approve",
               data={"draft_id": str(b), "subject": "  PaperPal   and agents ", "body": "Hi team,\r\n\r\n--my edit"},
               follow_redirects=False)
    assert r.status_code == 303 and "Approved" in r.headers["location"] and "generic" in r.headers["location"]
    assert quick[0] == ["approve-draft", "--draft", str(b), "--subject=PaperPal and agents", "--body=Hi team,\n\n--my edit"]

    other = c.post(f"/match/candidates/{cid}/drafts/{co}/approve", data={"draft_id": "999", "subject": "x", "body": "y"})
    assert other.status_code == 400  # a draft of another company/candidate
    assert c.post(f"/match/candidates/{cid}/drafts/{co}/approve",
                  data={"draft_id": str(b), "subject": "x", "body": "y" * 6000}).status_code == 400


def test_contacted_and_forget(drafted, quick):
    c, cid, co = drafted["client"], drafted["cid"], drafted["company"]
    c.post(f"/match/candidates/{cid}/drafts/{co}/contacted")
    assert quick[-1] == ["mark-contacted", "--candidate", str(cid), "--company", str(co)]
    contact_id = db.company_contacts(db.connect(__import__("sort_sherr").config.DB_PATH), co)[0]["id"]
    c.post(f"/match/candidates/{cid}/drafts/{co}/forget", data={"contact_id": str(contact_id)})
    assert quick[-1] == ["forget-contact", "--company", str(co), "--id", str(contact_id)]
    assert c.post(f"/match/candidates/{cid}/drafts/{co}/forget", data={"contact_id": "424242"}).status_code == 400


def test_download_needs_an_approved_variant(drafted):
    from sort_sherr import config

    c, cid, co = drafted["client"], drafted["cid"], drafted["company"]
    url = f"/match/candidates/{cid}/drafts/{co}/email.txt"
    assert c.get(url).status_code == 404
    db.approve_draft(db.connect(config.DB_PATH), drafted["draft_ids"][0], "Hi team,\n\nMy final text", "Final subject")
    r = c.get(url)
    assert r.status_code == 200 and r.text.startswith("To: founders@acme.ai\nFound at: https://acme.ai/about")
    assert "Subject: Final subject" in r.text and "My final text" in r.text


def test_unknown_candidate_or_company_404(drafted):
    c = drafted["client"]
    assert c.get("/match/candidates/999/drafts").status_code == 404
    assert c.get(f"/match/candidates/{drafted['cid']}/drafts/2").status_code == 404  # Beta has no drafts
