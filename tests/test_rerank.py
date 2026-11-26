"""LLM rerank + final score + a whole match run, with fake LLM and fake embedder (no network)."""

import asyncio

import pytest

from sort_sherr import match_config
from sort_sherr.matching import rank, rerank
from sort_sherr.matching.models import Evidence, Preferences, RerankBatch, RerankResult
from sort_sherr.matching.run import run_match
from sort_sherr.storage import db


class FakeLLM:
    """Answers every company in the prompt; can be told to skip a company once or return a bad job id."""

    model = "fake-llm"

    def __init__(self, skip_once=(), bad_job_for=()):
        self.calls, self.prompts = 0, []
        self.skip_once, self.bad_job_for = set(skip_once), set(bad_job_for)

    async def generate(self, prompt, schema):
        assert schema is RerankBatch
        self.calls += 1
        self.prompts.append(prompt)
        ids = [int(line.split(":")[1]) for line in prompt.splitlines() if line.startswith("company_id:")]
        results = []
        for cid in ids:
            if cid in self.skip_once:
                self.skip_once.discard(cid)
                continue
            job_line = next((l for l in prompt.split(f"company_id: {cid}")[1].splitlines() if l.startswith("- job ")), None)
            job_id = int(job_line.split()[2].rstrip(":")) if job_line else None
            results.append(RerankResult(
                company_id=cid, stack_overlap=15, domain_fit=cid % 20, interest_fit=10, seniority_fit=12, hiring_signal=99,
                why_fit="You built PaperPal with LangChain; they build things.",
                evidence=[
                    Evidence(resume_item="PaperPal RAG search with LangChain", company_fact=prompt.split(f"company_id: {cid}\nname: ")[1].split("\n")[0]),
                    Evidence(resume_item="great attitude", company_fact="amazing culture and vibes"),  # ungrounded
                ],
                best_job_id=999999 if cid in self.bad_job_for else job_id,
                why_this_role="Fits your stack." if job_id else None,
            ))
        return RerankBatch(results=results), {"prompt_tokens": 1000, "output_tokens": 300}


def companies(conn):
    return [c for c in db.matching_companies(conn) if c["is_hiring"] != 0]


def test_batches_cache_and_usage(match_db, profile, monkeypatch):
    monkeypatch.setattr(match_config, "RERANK_BATCH_SIZE", 2)
    cs = companies(match_db)  # 5 companies -> 3 batches
    llm = FakeLLM()
    results, usage = asyncio.run(rerank.rerank(match_db, llm, profile, Preferences(), cs))
    assert len(results) == 5 and llm.calls == 3
    assert usage["prompt_tokens"] == 3000 and usage["output_tokens"] == 900
    assert "Priya" not in "".join(llm.prompts)  # the name is never in the prompt

    # same inputs again -> all from cache, no calls
    llm2 = FakeLLM()
    _, usage2 = asyncio.run(rerank.rerank(match_db, llm2, profile, Preferences(), cs))
    assert llm2.calls == 0 and usage2["cached"] == 5

    # different preferences -> different answers needed -> cache miss
    asyncio.run(rerank.rerank(match_db, llm2, profile, Preferences(remote_only=True), cs[:1]))
    assert llm2.calls == 1


def test_skipped_company_retried_and_guards(match_db, profile):
    cs = companies(match_db)
    vf = next(c for c in cs if c["name"] == "VectorForge")
    llm = FakeLLM(skip_once={vf["id"]}, bad_job_for={vf["id"]})
    results, usage = asyncio.run(rerank.rerank(match_db, llm, profile, Preferences(), cs))
    r = results[vf["id"]]
    assert usage["llm_calls"] == 2  # second call only for the skipped company
    assert r.best_job_id is None and r.why_this_role is None  # 999999 isn't one of its jobs
    assert [e.company_fact for e in r.evidence] == ["VectorForge"]  # "amazing culture" dropped
    assert r.sub_scores()["hiring_signal"] == 20  # clamped to 0-20
    assert r.total() == 15 + vf["id"] % 20 + 10 + 12 + 20


def test_company_card_lists_job_ids(match_db):
    vf = next(c for c in companies(match_db) if c["name"] == "VectorForge")
    card = rerank.company_card(vf)
    assert f"- job {vf['jobs'][0]['id']}: Software Engineering Intern, Retrieval (internship, remote" in card


def test_final_score_formula(monkeypatch):
    company = {"yc_batch": None, "jobs": [{"is_internship": 1}]}
    assert rank.final_score(None, 0.5, company) == 50.0  # no rerank -> pure retrieval
    expected = match_config.W_RERANK * 80 + match_config.W_RETRIEVAL * 100 + match_config.BOOST_INTERN_POSTING
    assert rank.final_score(80, 1.0, company) == round(expected, 1)
    assert rank.normalize([0.5, 0.7, 0.6]) == pytest.approx([0.0, 1.0, 0.5])
    assert rank.normalize([0.4, 0.4]) == [1.0, 1.0]


def test_run_match_saves_run_and_every_pool_company(match_db, fake_embedder, profile):
    cid = db.save_candidate(match_db, "hash1", profile.model_dump_json(), "{}", "fake")
    prefs = Preferences(interests="robot deep learning, not crypto")
    run_id = asyncio.run(run_match(match_db, fake_embedder, FakeLLM(), cid, prefs, progress=lambda s: None))

    run = db.get_run(match_db, run_id)
    assert run["reranked"] == 1 and run["llm_model"] == "fake-llm" and run["embed_model"] == "fake-bow"
    assert run["prompt_tokens"] > 0 and run["cost_usd"] > 0
    rows = db.run_matches(match_db, run_id)
    assert len(rows) == 5  # every non-dropped company is in the (tiny) pool
    assert rows == sorted(rows, key=lambda m: -m["final_score"])
    vf = next(m for m in rows if m["company_name"] == "VectorForge")
    assert vf["job_title"] == "Software Engineering Intern, Retrieval" and vf["why_fit"]
    assert set(vf["sub_scores"]) == {"stack_overlap", "domain_fit", "interest_fit", "seniority_fit", "hiring_signal"}

    # a second run is stored separately, so runs can be compared
    run2 = asyncio.run(run_match(match_db, fake_embedder, None, cid, prefs, progress=lambda s: None))
    assert run2 != run_id and db.get_run(match_db, run2)["reranked"] == 0
    assert db.latest_run_id(match_db, cid) == run2


def test_changing_the_prompt_invalidates_the_cache(monkeypatch):
    key = rerank.cache_key("m", "student", "prefs", "card")
    monkeypatch.setattr(rerank, "PROMPT", rerank.PROMPT + "\nNew rule.")
    assert rerank.cache_key("m", "student", "prefs", "card") != key


# ---------- the two problems seen in the first real run ----------

CARD = """company_id: 7
name: Prior Labs
about: Foundation models for structured (tabular) data.
tags: AI, Machine Learning
team_size: 4
locations: Paris, Île-de-France, France, Remote
- job 70: Research Scientist Intern (PhD) - Cohort '27 (internship)
- job 71: ML Engineer (Paris)"""
STUDENT = "Skills: Python, LLMs, Node.js. Project Traffic sim: multi-agent LLM environment"


def result(**kw):
    base = dict(company_id=7, stack_overlap=10, domain_fit=10, interest_fit=10, seniority_fit=10, hiring_signal=10,
                why_fit="x", evidence=[])
    return RerankResult(**(base | kw))


def test_evidence_citing_team_size_or_location_is_dropped():
    r = result(evidence=[
        Evidence(resume_item="Node.js codebases", company_fact="team_size: 4"),
        Evidence(resume_item="Python, LLMs", company_fact="Paris, Île-de-France, France, Remote"),
        Evidence(resume_item="Traffic sim multi-agent LLM", company_fact="Foundation models for structured data"),
    ])
    r = rerank.clean_result(r, CARD, STUDENT, {70: "Research Scientist Intern (PhD)", 71: "ML Engineer (Paris)"}, False)
    assert [e.company_fact for e in r.evidence] == ["Foundation models for structured data"]


def test_phd_only_role_is_never_the_best_job_for_an_undergrad():
    jobs = {70: "Research Scientist Intern (PhD) - Cohort '27", 71: "ML Engineer"}
    r = rerank.clean_result(result(best_job_id=70, why_this_role="it's research"), CARD, STUDENT, jobs, False)
    assert r.best_job_id is None and r.why_this_role is None
    assert any("PhD" in f for f in r.red_flags)
    # a PhD student may take it
    r = rerank.clean_result(result(best_job_id=70), CARD, STUDENT, jobs, True)
    assert r.best_job_id == 70


def test_phd_only_internship_gets_no_intern_boost():
    phd = {"yc_batch": None, "jobs": [{"is_internship": 1, "title": "Research Intern (PhD)"}]}
    ug = {"yc_batch": None, "jobs": [{"is_internship": 1, "title": "Software Engineering Intern"}]}
    assert rank.boosts(phd) == 0
    assert rank.boosts(ug) == match_config.BOOST_INTERN_POSTING
