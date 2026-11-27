"""Part 4: reply-rate metrics, small-n honesty, the HTML report, CSV export, and follow-up drafts (fake LLM)."""

import asyncio
import csv
import io

import pytest
from typer.testing import CliRunner

from sort_sherr import cli, outreach_config, tracker_config
from sort_sherr.models import Company
from sort_sherr.outreach.models import Claim, GroundingReport
from sort_sherr.outreach.lint import lint
from sort_sherr.storage import db
from sort_sherr.tracker import lifecycle, metrics
from sort_sherr.tracker.followup_draft import FollowUpEmail, draft_followup


def at(day):
    return f"{day}T12:00:00+00:00"


def seed(conn, cand, n, replies, style="product-led", bounced=0, prefix="Co"):
    """n companies sent on Mon 2 Nov 2026; the first `replies` replied after 2 days, the next `bounced` bounced."""
    for i in range(n):
        co = db.upsert_company(conn, Company(name=f"{prefix}{i}", domain=f"{prefix.lower()}{i}.io", team_size=30))
        d = db.save_draft(conn, {"match_id": None, "candidate_id": cand, "company_id": co, "variant": "A", "style": style,
                                 "subject": "s", "body": "b", "facts_used": [], "student_items_used": [],
                                 "check_status": "ok", "check_notes": [], "lint_flags": []})
        oid = db.ensure_outreach(conn, cand, co, d)
        db.record_event(conn, oid, "sent", at("2026-11-02"), status="sent", channel="email")
        if i < replies:
            db.record_event(conn, oid, "replied", at("2026-11-04"), status="replied", reply_type="positive" if i % 2 == 0 else "neutral")
        elif i < replies + bounced:
            db.record_event(conn, oid, "bounced", at("2026-11-02"), status="bounced")


def test_rates_exclude_bounces_and_median(outreach_db):
    conn, cand = outreach_db["conn"], outreach_db["cand"]
    seed(conn, cand, 6, replies=2, bounced=2)
    s = metrics.summary(conn, cand)
    assert (s["sent"], s["bounced"], s["delivered"]) == (6, 2, 4)
    assert s["reply_rate"] == 0.5 and s["positive_rate"] == 0.25  # 2 of 4 delivered, 1 positive
    assert s["median_days_to_reply"] == 2.0
    assert dict(s["funnel"]) == {"drafted": 6, "sent": 6, "replied": 2, "interview": 0, "offer": 0}
    assert s["small"]  # 4 < 10


def test_small_groups_never_get_a_winner(outreach_db):
    conn, cand = outreach_db["conn"], outreach_db["cand"]
    seed(conn, cand, 4, replies=3, style="product-led", prefix="A")
    seed(conn, cand, 3, replies=0, style="project-led", prefix="B")
    variant = next(b for b in metrics.summary(conn, cand)["breakdowns"] if b["key"] == "variant")
    assert {g["group"]: (g["n"], g["replies"], g["small"]) for g in variant["groups"]} == \
        {"product-led": (4, 3, True), "project-led": (3, 0, True)}
    assert variant["verdict"].startswith("Too few to judge")


def test_big_groups_get_a_careful_leader(outreach_db):
    conn, cand = outreach_db["conn"], outreach_db["cand"]
    seed(conn, cand, 10, replies=4, style="product-led", prefix="A")
    seed(conn, cand, 10, replies=1, style="project-led", prefix="B")
    variant = next(b for b in metrics.summary(conn, cand)["breakdowns"] if b["key"] == "variant")
    assert "\"product-led\" leads with 40% (n=10)" in variant["verdict"]


def test_which_message_got_the_reply_and_dimensions(outreach_db):
    conn, cand = outreach_db["conn"], outreach_db["cand"]
    oid, _ = lifecycle.log_sent(conn, outreach_db["drafts"][0], at="2026-11-02")  # Monday
    db.record_event(conn, oid, "followup_sent", at("2026-11-09"), followup_number=1)
    lifecycle.log_reply(conn, oid, "positive", at="2026-11-10")
    [r] = metrics.records(conn, cand)
    assert (r["reply_after_followups"], r["weekday_sent"], r["recipient_role"]) == (1, "Mon", "founder")
    assert (r["yc_batch"], r["team_size"], r["contact_source"], r["variant"]) == ("YC 2025", "1-10", "first_party", "product-led")
    msg = next(b for b in metrics.summary(conn, cand)["breakdowns"] if b["key"] == "message")
    assert [(g["group"], g["n"], g["replies"]) for g in msg["groups"]] == \
        [("first email", 1, 0), ("follow-up 1", 1, 1), ("follow-up 2", 0, 0)]


@pytest.mark.parametrize("role,value,expected", [
    ("CEO", "a@x.io", "founder"), (None, "founders@x.io", "founder"), ("Engineering Manager", None, "hiring / eng lead"),
    (None, "careers@x.io", "hiring inbox"), (None, "hello@x.io", "general inbox"), (None, "ravi@x.io", "unknown"),
])
def test_recipient_role(role, value, expected):
    assert metrics.recipient_role({"contact_role": role, "contact_value": value, "contact_type": "email"}) == expected


def test_stats_report_export_commands(outreach_db, tmp_path):
    conn, cand = outreach_db["conn"], outreach_db["cand"]
    seed(conn, cand, 3, replies=1)
    r = CliRunner().invoke(cli.app, ["outreach-stats", "--candidate", str(cand)])
    assert r.exit_code == 0, r.output
    assert "Reply rate 33%" in r.output and "n<10" in r.output and "Too few to judge" in r.output

    out = tmp_path / "r" / "report.html"
    r = CliRunner().invoke(cli.app, ["report", "--candidate", str(cand), "--out", str(out)])
    assert r.exit_code == 0, r.output
    html = out.read_text()
    assert html.startswith("<!doctype html>") and "<script" not in html and "http" not in html.split("<main>")[1]
    assert "Funnel" in html and "33%" in html and "n&lt;10" in html

    csv_path = tmp_path / "e.csv"
    r = CliRunner().invoke(cli.app, ["export", "--candidate", str(cand), "--out", str(csv_path)])
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
    assert len(rows) == 3 and rows[0]["replied"] == "True" and "contact_value" not in rows[0]


def test_feedback_block_in_match_output(outreach_db):
    conn, cand = outreach_db["conn"], outreach_db["cand"]
    assert metrics.feedback(conn, cand) is None
    seed(conn, cand, 2, replies=1)
    f = metrics.feedback(conn, cand)
    assert f["delivered"] == 2 and f["replies"] == 1 and f["by"][0][0] == "team size"


# ---------- follow-up drafts ----------


class FollowupLLM:
    """Writes the follow-up and fact-checks it. bad_first: the first answer is a 'bump' with no new value."""

    model = "fake-llm"

    def __init__(self, bad_first=False):
        self.bad_first, self.prompts, self.writes = bad_first, [], 0

    async def generate(self, prompt, schema):
        self.prompts.append(prompt)
        if schema is FollowUpEmail:
            self.writes += 1
            if self.bad_first and self.writes == 1:
                return FollowUpEmail(body="Hi Ravi,\n\nJust following up on my email.\n\nThanks,\n[NAME]",
                                     facts_used=[], student_items_used=[]), {"prompt_tokens": 100, "output_tokens": 20}
            new = "F2" if "THE ONE NEW THING TO ADD: F2" in prompt else "S5"
            body = ("Hi Ravi,\n\nI saw you launched an open-source Python SDK. I wrapped my PaperPal parser around it "
                    "this weekend and it handled invoices into JSON well, which could help your users get started.\n\n"
                    "Would a short chat about the internship work?\n\nThanks,\n[NAME]")
            return FollowUpEmail(body=body, facts_used=["F1", "F2"] if new == "F2" else ["F1"],
                                 student_items_used=["S1"] if new == "F2" else ["S5"]), {"prompt_tokens": 100, "output_tokens": 30}
        return GroundingReport(claims=[Claim(variant="A", claim="launched an SDK", about="company", support="F2", supported=True)]), \
            {"prompt_tokens": 80, "output_tokens": 10}


def sent_outreach(outreach_db):
    conn = outreach_db["conn"]
    d = db.get_draft(conn, outreach_db["drafts"][0])
    cli.approve_and_export(conn, d, d["subject"], "Hi Ravi,\n\nAbout invoices.")
    oid, _ = lifecycle.log_sent(conn, d["id"], at="2026-11-02")
    return oid


def test_followup_uses_an_unused_recent_fact(outreach_db):
    conn = outreach_db["conn"]
    oid = sent_outreach(outreach_db)
    llm = FollowupLLM()
    d, usage = asyncio.run(draft_followup(conn, None, llm, oid))
    assert "THE ONE NEW THING TO ADD: F2" in llm.prompts[0] and "open-source Python SDK" in llm.prompts[0]
    assert "About invoices." in llm.prompts[0]  # the first email is shown, so it isn't repeated
    assert (d["followup_number"], d["outreach_id"], d["subject"]) == (1, oid, "Re: Invoices to JSON and PaperPal")
    assert d["body"].endswith("Priya Raman") and d["check_status"] == "ok", d["check_notes"]
    assert usage["llm_calls"] == 2  # write + check (the brief was saved: no research call)
    assert db.candidate_drafts(conn, outreach_db["cand"])[0]["followup_number"] == 0  # Part 3 lists stay first-email only
    with pytest.raises(ValueError, match="already drafted"):
        asyncio.run(draft_followup(conn, None, llm, oid))


def test_followup_with_your_value_and_a_bump_gets_rewritten(outreach_db):
    conn = outreach_db["conn"]
    oid = sent_outreach(outreach_db)
    llm = FollowupLLM(bad_first=True)
    d, usage = asyncio.run(draft_followup(conn, None, llm, oid, value="wrapped PaperPal around your SDK, see https://github.com/me/x"))
    assert "S5: New since the first email: wrapped PaperPal around your SDK" in llm.prompts[0]
    assert "github.com" not in llm.prompts[0]  # links in --value are redacted before the LLM sees them
    assert "just following up" in llm.prompts[2] or "bump" in llm.prompts[2].lower()  # rewrite got the problems
    assert llm.writes == 2 and d["check_status"] == "ok"


def test_followup_rules(outreach_db, monkeypatch):
    conn = outreach_db["conn"]
    oid = sent_outreach(outreach_db)
    lifecycle.log_reply(conn, oid, "neutral")
    with pytest.raises(ValueError, match="still waiting"):
        asyncio.run(draft_followup(conn, None, FollowupLLM(), oid))


def test_followups_count_toward_the_daily_cap(outreach_db, monkeypatch):
    from sort_sherr.outreach.run import drafts_left_today

    conn, cand = outreach_db["conn"], outreach_db["cand"]
    oid = sent_outreach(outreach_db)
    monkeypatch.setattr(outreach_config, "DAILY_DRAFT_CAP", 2)
    assert drafts_left_today(conn, cand) == 1  # Acme's first email today
    asyncio.run(draft_followup(conn, None, FollowupLLM(), oid))
    assert drafts_left_today(conn, cand) == 0
    db.record_event(conn, oid, "followup_sent", db.now(), followup_number=1)
    with pytest.raises(ValueError, match="daily cap"):
        asyncio.run(draft_followup(conn, None, FollowupLLM(), oid))


def test_lint_followup_mode():
    body = "Hi Ravi,\n\nJust checking in on my last note about the SDK.\n\nThanks,\nPriya"
    flags = lint("Re: a very long subject line about many many things here", body, followup=True)
    assert any(f.startswith("length") for f in flags) and any("checking in" in f for f in flags)
    assert not any(f.startswith("subject") for f in flags)
    assert tracker_config.FOLLOWUP_MAX_WORDS == 60
