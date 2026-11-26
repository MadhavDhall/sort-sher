"""Hard filters + embedding retrieval, with a fake bag-of-words embedder (no model download)."""

from sort_sherr import match_config
from sort_sherr.matching.embed import embed_cached
from sort_sherr.matching.models import Preferences
from sort_sherr.matching.retrieve import company_document, hard_filter, retrieve
from sort_sherr.storage import db


def by_name(companies):
    return {c["name"]: c for c in companies}


def test_matching_companies_attach_active_jobs(match_db):
    cs = by_name(db.matching_companies(match_db))
    assert [j["title"] for j in cs["VectorForge"]["jobs"]][0] == "Software Engineering Intern, Retrieval"  # interns first
    assert cs["QuietCo"]["jobs"] == []


def test_filter_drops_known_not_hiring_but_keeps_unknown(match_db):
    kept, stats = hard_filter(db.matching_companies(match_db), Preferences())
    names = {c["name"] for c in kept}
    assert "QuietCo" not in names  # YC says not hiring, no jobs
    assert "MysteryLabs" in names  # is_hiring unknown -> kept
    assert stats["dropped: not hiring"] == 1 and stats["kept_unknown"] == 1


def test_remote_only_drops_only_known_onsite(match_db):
    kept, stats = hard_filter(db.matching_companies(match_db), Preferences(remote_only=True))
    names = {c["name"] for c in kept}
    assert "ShopStack" not in names and "Sales Rocket" not in names  # all jobs known on-site
    assert {"VectorForge", "ChainPay"} <= names  # have a remote job
    assert "MysteryLabs" in names  # no jobs -> unknown -> kept
    assert stats["dropped: on-site only"] == 2


def test_location_filter_keeps_remote_and_unknown(match_db):
    kept, _ = hard_filter(db.matching_companies(match_db), Preferences(locations=["India"]))
    names = {c["name"] for c in kept}
    assert "ShopStack" in names  # Bangalore, India
    assert "VectorForge" in names  # SF, but has a remote job
    assert "MysteryLabs" in names  # location unknown
    assert "Sales Rocket" not in names  # New York only


def test_role_filter(match_db):
    kept, _ = hard_filter(db.matching_companies(match_db), Preferences(roles=["sde"]))
    names = {c["name"] for c in kept}
    assert "Sales Rocket" not in names  # only an Account Executive role
    assert "MysteryLabs" in names  # no jobs -> unknown -> kept


def test_embedding_cache_only_embeds_changes(match_db, fake_embedder):
    texts = ["python backend", "react frontend"]
    _, new = embed_cached(match_db, fake_embedder, texts)
    assert new == 2
    _, new = embed_cached(match_db, fake_embedder, texts)
    assert new == 0 and fake_embedder.embedded == 2  # served from the DB
    _, new = embed_cached(match_db, fake_embedder, ["python backend", "react frontend and crypto"])
    assert new == 1


def test_retrieve_ranks_relevant_company_first(match_db, fake_embedder, profile):
    kept, _ = hard_filter(db.matching_companies(match_db), Preferences())
    pool, _ = retrieve(match_db, fake_embedder, profile, Preferences(), kept)
    names = [m["company"]["name"] for m in pool]
    assert set(names[:2]) == {"VectorForge", "ShopStack"}  # both share the student's stack
    assert names.index("Sales Rocket") > 1
    assert all(m["found_by"] == "resume" for m in pool)
    assert pool == sorted(pool, key=lambda m: -m["retrieval_score"])


def test_interests_add_extra_companies(match_db, fake_embedder, profile, monkeypatch):
    monkeypatch.setattr(match_config, "pool_size", lambda shortlist: 2)
    monkeypatch.setattr(match_config, "TOP_K_INTERESTS", 1)
    kept, _ = hard_filter(db.matching_companies(match_db), Preferences())

    pool, _ = retrieve(match_db, fake_embedder, profile, Preferences(), kept)
    assert "MysteryLabs" not in {m["company"]["name"] for m in pool}

    prefs = Preferences(interests="I want to work on robot deep learning")
    pool, _ = retrieve(match_db, fake_embedder, profile, prefs, kept)
    found = {m["company"]["name"]: m["found_by"] for m in pool}
    assert found["MysteryLabs"] == "interests"  # added only because of what the student said they want
    assert len(pool) == 3


def test_pool_grows_with_shortlist():
    assert match_config.pool_size(15) == match_config.TOP_K_RESUME  # never below the default
    assert match_config.pool_size(100) == 150  # 1.5x, so the LLM can push weak ones out
    assert match_config.pool_size(1000) == match_config.MAX_POOL


def test_company_document_includes_jobs(match_db):
    doc = company_document(by_name(db.matching_companies(match_db))["VectorForge"])
    assert "Retrieval infrastructure" in doc and "Software Engineering Intern" in doc and "LangChain" in doc
