"""Part 3, step C: drafting + grounding check + the draft run (fake LLM, fake client, no network)."""

import asyncio
import json

import pytest

from conftest import FakeClient
from sort_sherr import outreach_config
from sort_sherr.matching.models import CandidateProfile, Preferences, Project
from sort_sherr.models import Company
from sort_sherr.outreach import draft, run as outreach_run
from sort_sherr.outreach.models import (Claim, CompanyBrief, DraftPair, EmailVariant, Fact, GroundingReport,
                                        numbered_facts)
from sort_sherr.storage import db

BODY = ("Hi Carlos,\n\nYour Launch YC post says AfterQuery curates expert training data for frontier AI labs. "
        "That is close to PaperPal, my RAG search over arXiv papers built with LangChain and Python, where answer "
        "quality only improved once I cleaned and labeled the source passages by hand. I would like to ask about "
        "a {role} for Summer 2027, remote. I could help build tooling for your expert data pipeline or "
        "evaluation scripts for the agent environments you describe. Would a short call in the next few weeks "
        "work? If email is easier, I am glad to answer any questions here.\n\nThanks,\n[NAME]\n[GITHUB]")


def variant(style, body=BODY, facts=("F1",), items=("S1",), subject="PaperPal and expert training data"):
    return EmailVariant(style=style, subject=subject, body=body.format(role="software engineering internship"),
                        facts_used=list(facts), student_items_used=list(items))


class DraftLLM:
    """Plays all three roles: brief writer, email writer and grounding checker.
    unsupported: list of variants whose claims are rejected, one list per check call (then all pass)."""

    model = "fake-llm"

    def __init__(self, unsupported=(), first_pair=None):
        self.unsupported = list(unsupported)
        self.first_pair = first_pair
        self.calls = {"CompanyBrief": 0, "DraftPair": 0, "GroundingReport": 0}
        self.prompts = []

    async def generate(self, prompt, schema):
        self.calls[schema.__name__] += 1
        self.prompts.append(prompt)
        if schema is CompanyBrief:
            return CompanyBrief(
                what_they_build=[Fact(text="Curates expert training data for frontier AI labs", source_url="https://afterquery.com")],
                recent_updates=[Fact(text="Raised at a $3.2B valuation", date="Sep 2026",
                                     source_url="https://www.forbes.com/sites/annatong/2026/09/01/afterquery-becomes-ycs-fastest-unicorn-at-32-billion/")],
            ), {"prompt_tokens": 100, "output_tokens": 10}
        if schema is DraftPair:
            if self.first_pair and self.calls["DraftPair"] == 1:
                return self.first_pair, {"prompt_tokens": 200, "output_tokens": 50}
            return DraftPair(variants=[variant("product-led"), variant("project-led")]), {"prompt_tokens": 200, "output_tokens": 50}
        bad = self.unsupported.pop(0) if self.unsupported else []
        claims = [Claim(variant=v, claim="AfterQuery curates expert training data", about="company",
                        support=None if v in bad else "F1", supported=v not in bad) for v in ("A", "B")]
        claims.append(Claim(variant="A", claim="Would a short call work?", about="other", support=None, supported=True))
        return GroundingReport(claims=claims), {"prompt_tokens": 150, "output_tokens": 20}


@pytest.fixture
def student():
    profile = CandidateProfile(
        name="Priya Raman", links=["https://github.com/priya-r", "https://linkedin.com/in/priya-r"],
        skills=["Python", "LangChain"],
        projects=[Project(title="PaperPal", description="RAG search over arXiv papers", tech_stack=["LangChain", "Python"])],
    )
    return profile, {"name": "Priya Raman", "emails": ["priya@example.org"], "links": profile.links}


BRIEF = CompanyBrief(what_they_build=[Fact(text="Curates expert training data for frontier AI labs", source_url="https://afterquery.com")])
MATCH = {"why_fit": "You built a RAG system; they make training data.", "reasons": [], "job_title": None,
         "pitch_role": "Software engineering intern"}
ASK = {"role": "Software engineering intern", "window": "Summer 2027", "setup": "remote"}


def run_draft(llm, student, other=()):
    profile, pii = student
    return asyncio.run(draft.draft_company(llm, profile=profile, pii=pii, company_name="AfterQuery", brief=BRIEF,
                                           match=MATCH, ask=ASK, recipient={"person_name": "Carlos Georgescu",
                                           "person_role": "Founder/CTO"}, other_drafts=list(other)))


def test_happy_path_fills_links_locally(student):
    llm = DraftLLM()
    drafts, usage = run_draft(llm, student)
    assert [d["variant"] for d in drafts] == ["A", "B"] and all(d["check_status"] == "ok" for d in drafts)
    body = drafts[0]["body"]
    assert "Priya Raman" in body and "https://github.com/priya-r" in body and "[NAME]" not in body
    assert drafts[0]["facts_used"][0]["source_url"] == "https://afterquery.com"  # shows where the fact came from
    assert drafts[0]["student_items_used"][0]["text"].startswith('Project "PaperPal"')
    everything_sent = "\n".join(llm.prompts)
    assert "Priya" not in everything_sent and "priya-r" not in everything_sent  # name + links never sent
    assert "[GITHUB] [LINKEDIN]" in llm.prompts[0]
    assert usage["llm_calls"] == 2  # write + check


def test_regenerates_once_then_ok(student):
    llm = DraftLLM(unsupported=[["A"]])  # first check rejects A, the rewrite passes
    drafts, usage = run_draft(llm, student)
    assert llm.calls["DraftPair"] == 2 and llm.calls["GroundingReport"] == 2
    assert "YOUR PREVIOUS DRAFT HAD THESE PROBLEMS" in llm.prompts[2] and "Variant A" in llm.prompts[2]
    assert all(d["check_status"] == "ok" for d in drafts)


def test_fails_twice_needs_review(student):
    llm = DraftLLM(unsupported=[["B"], ["B"]])
    drafts, _ = run_draft(llm, student)
    a, b = drafts
    assert a["check_status"] == "ok" and b["check_status"] == "needs_review"
    assert any("unsupported company claim" in n for n in b["check_notes"])
    assert llm.calls["DraftPair"] == 2  # only ONE rewrite


def test_rule_checks_catch_made_up_numbers_and_ids(student):
    bad = variant("product-led", body=BODY.replace("by hand", "by hand for 10,000 users"), facts=("F9",))
    llm = DraftLLM(first_pair=DraftPair(variants=[bad, variant("project-led")]))
    drafts, _ = run_draft(llm, student)
    feedback = llm.prompts[2]
    assert "the number 10,000 is not in any fact or resume item" in feedback
    assert "cites ids that don't exist: F9" in feedback
    assert drafts[0]["check_status"] == "ok"  # the rewrite was clean


def test_no_sourced_facts_no_draft(student):
    profile, pii = student
    with pytest.raises(ValueError, match="no sourced facts"):
        asyncio.run(draft.draft_company(DraftLLM(), profile=profile, pii=pii, company_name="X", brief=CompanyBrief(what_they_build=[]),
                                        match=MATCH, ask=ASK, recipient=None, other_drafts=[]))


# ---------- the whole draft run: cap, contacted, already drafted ----------


@pytest.fixture
def two_companies(afterquery_db, student, load_fixture):
    conn = afterquery_db
    acme = db.upsert_company(conn, Company(name="Acme", website="https://acme.dev", domain="acme.dev", sources=["hn"]))
    profile, pii = student
    cand = db.save_candidate(conn, "hash", profile.model_dump_json(), json.dumps(pii), "fake")
    rows = [{"company_id": cid, "found_by": "resume", "retrieval_score": 0.5, "rerank_score": 80, "final_score": score,
             "sub_scores": "{}", "why_fit": "fits", "pitch_role": "SWE intern", "reasons": "[]", "red_flags": "[]"}
            for cid, score in [(1, 90), (acme, 80)]]
    run_id = db.save_match_run(conn, cand, {"prefs_json": Preferences(window="Summer 2027").model_dump_json(),
                                            "reranked": 1}, rows)
    client = FakeClient({
        "https://afterquery.com": load_fixture("research_home.html"),
        "https://www.ycombinator.com/companies/afterquery": load_fixture("yc_company_page.html"),
        "https://hn.algolia.com/api/v1/search*": {"hits": []},
        "https://acme.dev": "<html><body><h1>Acme</h1><p>Invoices to JSON.</p><a href='mailto:jobs@acme.dev'>jobs</a></body></html>",
    })
    return conn, cand, acme, client


async def mx_yes(domain):
    return True


def draft_run(conn, cand, client, **kw):
    return asyncio.run(outreach_run.draft_for_candidate(conn, client, DraftLLM(), cand, top=5, window="Summer 2027",
                                                        setup="remote", mx_check=mx_yes, progress=lambda *_: None, **kw))


def test_draft_run_cap_contacted_and_redo(two_companies, monkeypatch):
    conn, cand, acme, client = two_companies
    monkeypatch.setattr(outreach_config, "DAILY_DRAFT_CAP", 1)
    out = draft_run(conn, cand, client)
    assert [r["status"] for r in out["results"]] == ["drafted", "skipped: daily cap of 1 reached"]
    assert len(db.candidate_drafts(conn, cand)) == 2  # A + B for the first company
    assert conn.execute("SELECT llm_calls FROM outreach_runs").fetchone()[0] == 3  # brief + write + check

    monkeypatch.setattr(outreach_config, "DAILY_DRAFT_CAP", 10)
    db.mark_contacted(conn, cand, acme)
    out = draft_run(conn, cand, client)
    assert [r["status"] for r in out["results"]] == ["skipped: already drafted (use --redo)", "skipped: already contacted"]

    out = draft_run(conn, cand, client, redo=True)
    assert out["results"][0]["status"] == "drafted"
    assert len(db.candidate_drafts(conn, cand)) == 2  # old drafts replaced, not added


def test_drafts_store_what_review_needs(two_companies):
    conn, cand, acme, client = two_companies
    draft_run(conn, cand, client)
    d = db.candidate_drafts(conn, cand)[0]
    assert d["match_id"] and d["variant"] == "A" and d["check_status"] == "ok"
    assert d["facts_used"][0]["id"] == "F1" and d["facts_used"][0]["source_url"]
    db.approve_draft(conn, d["id"], d["body"] + "\nP.S. edited")
    chosen = [x for x in db.candidate_drafts(conn, cand) if x["chosen"]]
    assert len(chosen) == 1 and chosen[0]["edited_body"].endswith("edited") and chosen[0]["approved_at"]


def test_no_send_code_anywhere():
    """Sort Sherr only drafts. No SMTP, no mail-sending API, anywhere in the package."""
    from pathlib import Path

    import sort_sherr

    code = "\n".join(p.read_text() for p in Path(sort_sherr.__file__).parent.rglob("*.py"))
    for forbidden in ("smtplib", "sendmail", "send_message", "messages.send", "users().messages", "SMTP("):
        assert forbidden not in code, forbidden


def test_link_placeholders_filled_or_removed():
    values = {"[NAME]": "Priya Raman", "[GITHUB]": "https://github.com/priya-r"}
    text = "Thanks,\n[NAME]\n[GITHUB_LINK]\n[Resume Link]\n(no links)"
    assert draft.fill_placeholders(text, values) == "Thanks,\nPriya Raman\nhttps://github.com/priya-r"


def test_no_links_means_no_link_lines_in_prompt(student):
    profile, pii = student
    profile.links, pii = [], {"name": "Priya Raman", "links": []}
    llm = DraftLLM()
    asyncio.run(draft.draft_company(llm, profile=profile, pii=pii, company_name="AfterQuery", brief=BRIEF,
                                    match=MATCH, ask=ASK, recipient=None, other_drafts=[]))
    assert "NO links or link placeholders" in llm.prompts[0] and "(no links)" not in llm.prompts[0]


def test_links_saved_in_pii_are_used(student):
    profile, _ = student
    profile.links = []
    values = draft.link_values(profile, {"name": "P R", "links": ["https://github.com/pr", "https://pr.dev"]})
    assert values["[GITHUB]"] == "https://github.com/pr" and values["[PORTFOLIO]"] == "https://pr.dev"


def test_ask_is_an_internship_not_a_full_time_role():
    full_time = {"job_title": "Member of Technical Staff", "job_is_internship": 0, "pitch_role": "Applied AI intern"}
    assert outreach_run.build_ask(full_time, "Summer 2027", "remote", [])["role"] == "Applied AI intern"
    intern = {"job_title": "Software Engineering Intern", "job_is_internship": 1, "pitch_role": "x"}
    assert outreach_run.build_ask(intern, "Summer 2027", "remote", [])["role"] == "Software Engineering Intern"
