"""Part 3: draft eval (judge vs. your ratings) and the review CLI (fake editor, temp DB, no network)."""

import asyncio
import csv

import pytest
from typer.testing import CliRunner

from sort_sherr import cli, config, outreach_config
from sort_sherr.models import Company
from sort_sherr.outreach import judge
from sort_sherr.outreach.models import JudgeScores
from sort_sherr.storage import db


class JudgeLLM:
    model = "fake-llm"

    async def generate(self, prompt, schema):
        assert schema is JudgeScores and "Subject:" in prompt and "F1:" in prompt
        return JudgeScores(specificity=4, grounding=5, relevance=3, length=5, tone=9, comment="none"), \
            {"prompt_tokens": 300, "output_tokens": 30}


@pytest.fixture
def drafts_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(outreach_config, "DRAFTS_DIR", tmp_path / "drafts")
    conn = db.connect(path)
    cid = db.upsert_company(conn, Company(name="Acme Labs", domain="acme.dev"))
    cand = db.save_candidate(conn, "h", "{}", "{}", "fake")
    fact = {"id": "F1", "section": "what_they_build", "text": "Turns invoices into JSON", "source_url": "https://acme.dev", "date": None}
    for v in ("A", "B"):
        db.save_draft(conn, {"match_id": None, "candidate_id": cand, "company_id": cid, "variant": v, "style": "x",
                             "subject": f"Invoices to JSON {v}", "body": f"Hi Ravi,\nbody {v}", "facts_used": [fact],
                             "student_items_used": [], "check_status": "ok", "check_notes": [], "lint_flags": []})
    db.save_contacts(conn, cid, [{"type": "email", "value": "ravi@acme.dev", "person_name": "Ravi Kumar",
                                  "person_role": "CTO", "source_type": "first_party", "source_url": "https://acme.dev/team",
                                  "confidence": "high", "mx_ok": 1}])
    return conn, cand, cid


def test_judge_csv_keeps_my_ratings_and_reports(drafts_db, tmp_path):
    conn, cand, _ = drafts_db
    drafts = judge.pick_drafts(conn, cand, 10)
    scores = [asyncio.run(judge.judge_draft(JudgeLLM(), d))[0] for d in drafts]
    assert scores[0].average() == 4.4  # tone 9 is clamped to 5
    path = tmp_path / "ratings.csv"
    judge.write_csv(path, judge.judge_rows(drafts, scores))

    rows = list(csv.DictReader(open(path)))
    rows[0]["my_rating"], rows[1]["my_rating"] = "4", "2"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=judge.COLUMNS)
        w.writeheader()
        w.writerows(rows)
    judge.write_csv(path, judge.judge_rows(drafts, scores))  # judging again keeps my ratings
    r = judge.report(path)
    assert r["rated"] == 2 and r["within_1"] == 1
    assert r["mean_abs_diff"] == pytest.approx((0.4 + 2.4) / 2)
    assert r["correlation"] is None  # fewer than 3 ratings


def test_pearson():
    assert judge.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert judge.pearson([1, 1, 1], [1, 2, 3]) is None


def test_review_pick_edit_approve_export(drafts_db, monkeypatch, tmp_path):
    conn, cand, cid = drafts_db
    import click

    monkeypatch.setattr(click, "edit", lambda text, extension=None: text.replace("body B", "my own words for Acme"))
    result = CliRunner().invoke(cli.app, ["review", "--candidate", str(cand)], input="b\na\n")
    assert result.exit_code == 0, result.output
    assert "found at: https://acme.dev/team" in result.output  # contact shown with its source
    assert "F1: Turns invoices into JSON" in result.output and "https://acme.dev" in result.output

    chosen = [d for d in db.candidate_drafts(conn, cand) if d["chosen"]]
    assert [d["variant"] for d in chosen] == ["B"] and "my own words" in chosen[0]["edited_body"]
    exported = (tmp_path / "drafts" / "acme-labs.txt").read_text()
    assert exported.startswith("To: ravi@acme.dev  (Ravi Kumar, CTO)\nFound at: https://acme.dev/team")
    assert "Subject: Invoices to JSON B" in exported and "my own words for Acme" in exported

    again = CliRunner().invoke(cli.app, ["review", "--candidate", str(cand)])
    assert "Nothing to review" in again.output  # approved companies don't come back


def test_forget_and_mark_contacted_commands(drafts_db):
    conn, cand, cid = drafts_db
    out = CliRunner().invoke(cli.app, ["forget-contact", "--company", str(cid)], input="y\n")
    assert out.exit_code == 0 and "Forgot 1 contact" in out.output
    assert db.company_contacts(conn, cid) == []
    out = CliRunner().invoke(cli.app, ["mark-contacted", "--candidate", str(cand), "--company", str(cid)])
    assert out.exit_code == 0 and db.contacted_company_ids(conn, cand) == {cid}
