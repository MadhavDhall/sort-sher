"""Part 4: outreach records, the event log, migration from Part 3's `contacted`, manual logging, forget."""

import pytest
from typer.testing import CliRunner

from sort_sherr import cli
from sort_sherr.storage import db
from sort_sherr.tracker import lifecycle


def kinds(conn, oid):
    return [e["kind"] for e in db.outreach_events(conn, oid)]


# ---------- data model ----------


def test_check_constraint_rejects_unknown_status(outreach_db):
    conn = outreach_db["conn"]
    oid = db.ensure_outreach(conn, outreach_db["cand"], outreach_db["company"])
    with pytest.raises(db.INTEGRITY_ERRORS):
        conn.execute("UPDATE outreach SET status = 'maybe' WHERE id = ?", (oid,))
    with pytest.raises(db.INTEGRITY_ERRORS):  # foreign keys are enforced too
        conn.execute("INSERT INTO outreach_events (outreach_id, kind, at) VALUES (999, 'sent', 'x')")


def test_migrates_old_contacted_rows_once(tmp_path):
    """A Part 3 database with a `contacted` row becomes an outreach with a 'sent' event, note kept."""
    path = tmp_path / "old.db"
    conn = db.connect(path)
    from sort_sherr.models import Company

    company = db.upsert_company(conn, Company(name="Old Co", domain="old.co"))
    cand = db.save_candidate(conn, "h", "{}", "{}", "fake")
    conn.execute("INSERT INTO contacted VALUES (?, ?, '2026-11-20T10:00:00+00:00', 'emailed the founder')", (cand, company))
    conn.commit()
    conn.close()

    for _ in range(2):  # reconnecting doesn't migrate twice
        conn = db.connect(path)
        rows = db.candidate_outreach(conn, cand)
        assert len(rows) == 1 and rows[0]["status"] == "sent"
        events = db.outreach_events(conn, rows[0]["id"])
        assert [(e["kind"], e["source"], e["note"], e["at"]) for e in events] == \
            [("sent", "migrated", "emailed the founder", "2026-11-20T10:00:00+00:00")]
        assert db.contacted_company_ids(conn, cand) == {company}  # Part 3's skip rule reads outreach now
        conn.close()


def test_research_refresh_keeps_the_contact_you_wrote_to(outreach_db):
    conn, company = outreach_db["conn"], outreach_db["company"]
    ravi = db.company_contacts(conn, company)[0]
    oid, _ = lifecycle.log_sent(conn, outreach_db["drafts"][0])
    assert db.get_outreach(conn, oid)["contact_id"] == ravi["id"]
    new = [{"type": "email", "value": "ravi@acme.dev", "source_type": "first_party", "source_url": "https://acme.dev/team",
            "confidence": "high"},
           {"type": "email", "value": "jobs@acme.dev", "source_type": "first_party", "source_url": "https://acme.dev/jobs",
            "confidence": "high"}]
    assert db.save_contacts(conn, company, new) == 2  # same count as before Part 4
    assert db.get_outreach(conn, oid)["contact_value"] == "ravi@acme.dev"  # link still valid
    assert {c["value"] for c in db.company_contacts(conn, company)} == {"ravi@acme.dev", "jobs@acme.dev"}


# ---------- lifecycle ----------


def test_approve_creates_drafted_outreach_then_sent(outreach_db):
    conn, cand, company = outreach_db["conn"], outreach_db["cand"], outreach_db["company"]
    d = db.get_draft(conn, outreach_db["drafts"][1])
    cli.approve_and_export(conn, d, d["subject"], "Hi Ravi,\n\nmy words")
    oid = db.find_outreach(conn, cand, company)
    o = db.get_outreach(conn, oid)
    assert o["status"] == "drafted" and o["draft_id"] == d["id"] and o["contact_value"] == "ravi@acme.dev"
    assert company not in db.contacted_company_ids(conn, cand)  # approved is not contacted

    lifecycle.log_sent(conn, d["id"], at="2026-11-20 10:30")
    o = db.get_outreach(conn, oid)
    assert (o["status"], o["channel"], o["draft_style"]) == ("sent", "email", "project-led")
    assert kinds(conn, oid) == ["drafted", "sent"]
    assert company in db.contacted_company_ids(conn, cand)


def test_reply_interview_offer_and_not_allowed_moves(outreach_db):
    conn = outreach_db["conn"]
    oid, _ = lifecycle.log_sent(conn, outreach_db["drafts"][0], at="2026-11-20")
    with pytest.raises(ValueError, match="log the reply first"):
        lifecycle.set_status(conn, oid, "interview")
    with pytest.raises(ValueError, match="already|can't go"):
        lifecycle.log_sent(conn, outreach_db["drafts"][0])  # can't send twice
    lifecycle.log_reply(conn, oid, "positive", note="wants a call")
    lifecycle.set_status(conn, oid, "answered")
    lifecycle.set_status(conn, oid, "interview", when="2030-01-05 15:00")
    lifecycle.set_status(conn, oid, "offer")
    o = db.get_outreach(conn, oid)
    assert (o["status"], o["reply_type"]) == ("offer", "positive")
    assert kinds(conn, oid) == ["drafted", "sent", "replied", "answered", "interview", "offer"]
    with pytest.raises(ValueError):
        lifecycle.log_reply(conn, oid, "neutral")  # offer is final


def test_parse_when():
    assert lifecycle.parse_when("2026-11-20 10:30").startswith("2026-11-2")
    with pytest.raises(ValueError, match="future"):
        lifecycle.parse_when("2099-01-01")
    assert lifecycle.parse_when("2099-01-01", allow_future=True).startswith("2099")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        lifecycle.parse_when("next tuesday")


def test_contact_must_belong_to_the_company(outreach_db):
    with pytest.raises(ValueError, match="not one of"):
        lifecycle.log_sent(outreach_db["conn"], outreach_db["drafts"][0], contact_id=999)


def test_mark_contacted_still_works(outreach_db):
    conn, cand, company = outreach_db["conn"], outreach_db["cand"], outreach_db["company"]
    db.mark_contacted(conn, cand, company, "sent from my phone")
    db.mark_contacted(conn, cand, company)  # twice = once
    oid = db.find_outreach(conn, cand, company)
    assert kinds(conn, oid) == ["drafted", "sent"] and db.get_outreach(conn, oid)["channel"] == "unknown"


# ---------- CLI ----------


def run(*args):
    result = CliRunner().invoke(cli.app, [str(a) for a in args])
    return result


def test_cli_sent_reply_status_outreach(outreach_db):
    conn, cand = outreach_db["conn"], outreach_db["cand"]
    r = run("sent", outreach_db["drafts"][0], "--channel", "linkedin", "--at", "2026-11-20")
    assert r.exit_code == 0, r.output
    oid = db.find_outreach(conn, cand, outreach_db["company"])
    assert "via linkedin" in r.output and f"outreach #{oid}" in r.output
    r = run("reply", oid, "--type", "neutral", "--note=asked for a resume")
    assert r.exit_code == 0, r.output
    r = run("status", oid, "rejected")
    assert r.exit_code == 0
    r = run("status", oid, "interview")
    assert r.exit_code != 0 and "can't go" in r.output  # rejected is final: a clear error
    r = run("outreach", "--candidate", cand)
    assert "Acme Labs" in r.output and "rejected (neutral)" in r.output
    assert run("reply", oid, "--type", "great").exit_code != 0


def test_forget_company_deletes_personal_data(outreach_db):
    conn, cand, company, tmp = outreach_db["conn"], outreach_db["cand"], outreach_db["company"], outreach_db["tmp"]
    d = db.get_draft(conn, outreach_db["drafts"][0])
    cli.approve_and_export(conn, d, d["subject"], "Hi Ravi,\n\nhello")
    lifecycle.log_sent(conn, d["id"])
    assert (tmp / "drafts" / "acme-labs.txt").exists()
    r = run("forget", "--company", company, "--yes")
    assert r.exit_code == 0, r.output
    assert db.candidate_outreach(conn, cand) == [] and db.candidate_drafts(conn, cand) == []
    assert db.company_contacts(conn, company) == [] and not (tmp / "drafts" / "acme-labs.txt").exists()
    assert db.get_company(conn, company) is not None  # public data stays
    assert conn.execute("SELECT count(*) FROM outreach_events").fetchone()[0] == 0  # cascade


def test_forget_contact_keeps_history(outreach_db):
    conn = outreach_db["conn"]
    oid, _ = lifecycle.log_sent(conn, outreach_db["drafts"][0])
    r = run("forget", "--contact=RAVI@acme.dev", "--yes")
    assert r.exit_code == 0, r.output
    o = db.get_outreach(conn, oid)
    assert o["status"] == "sent" and o["contact_id"] is None  # history kept, address gone
    assert db.save_contacts(conn, outreach_db["company"], [{"type": "email", "value": "ravi@acme.dev",
        "source_type": "first_party", "source_url": "https://acme.dev/team", "confidence": "high"}]) == 0
    assert run("forget", "--yes").exit_code != 0  # needs one of the two
