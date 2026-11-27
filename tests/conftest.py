import json
from pathlib import Path

import numpy as np
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def load(name: str):
        text = (FIXTURES / name).read_text()
        return json.loads(text) if name.endswith(".json") else text

    return load


# ---------- Part 2 helpers ----------


class FakeEmbedder:
    """Deterministic bag-of-words 'embedding' over a small vocabulary: no model download, no network.
    Texts sharing words get a higher cosine similarity, which is all retrieval needs."""

    name = "fake-bow"
    VOCAB = ["python", "pytorch", "langchain", "rag", "retrieval", "vector", "llm", "fastapi", "postgresql",
             "backend", "react", "frontend", "sales", "crypto", "robot", "deep", "payroll", "docker", "intern"]

    def __init__(self):
        self.embedded = 0  # how many texts we were asked to embed (to test the cache)

    def embed(self, texts, is_query=False):
        self.embedded += len(texts)
        rows = []
        for t in texts:
            low = t.lower()
            v = np.array([low.count(w) for w in self.VOCAB], dtype=np.float32) + 0.01
            rows.append(v / np.linalg.norm(v))
        return np.vstack(rows)


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()


@pytest.fixture
def match_db(load_fixture):
    """In-memory DB with the fake companies + jobs from companies_fake.json."""
    from sort_sherr.models import Company, JobDraft
    from sort_sherr.storage import db

    conn = db.connect(":memory:")
    for item in load_fixture("companies_fake.json"):
        cid = db.upsert_company(conn, Company(**item["company"]))
        if item["jobs"]:
            db.save_jobs(conn, cid, 1, [JobDraft(**j) for j in item["jobs"]])
    return conn


@pytest.fixture
def profile():
    from sort_sherr.matching.models import CandidateProfile, Experience, Project

    return CandidateProfile(
        name="Priya Raman",
        skills=["Python", "PyTorch", "LangChain", "FastAPI", "PostgreSQL"],
        projects=[
            Project(title="PaperPal", description="RAG retrieval search over arXiv papers", tech_stack=["LangChain", "Python"]),
            Project(title="ShopAPI", description="backend for a campus store", tech_stack=["FastAPI", "PostgreSQL"]),
        ],
        experience=[Experience(role="ML Intern", organization="Acme", tech_stack=["PyTorch"])],
        focus_areas=["GenAI", "backend"],
    )


# ---------- Part 3 helpers ----------


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """Tests never open a real (Playwright) browser: that would be a live network call."""
    from sort_sherr.extractors import browser

    monkeypatch.setattr(browser, "available", lambda: False)


class FakeResponse:
    def __init__(self, url, text):
        import json as _json

        self.url = url
        self.text = text if isinstance(text, str) else _json.dumps(text)  # load_fixture parses .json files

    def json(self):
        import json as _json

        return _json.loads(self.text)


class FakeClient:
    """Stands in for PoliteClient: serves saved pages by URL, FetchError("http_404") for anything else.
    Records every URL asked for, so tests can check what was (not) fetched."""

    def __init__(self, pages: dict[str, str]):
        self.pages, self.fetched = pages, []

    async def get(self, url):
        from sort_sherr.http import FetchError

        self.fetched.append(url)
        for key, text in self.pages.items():
            if url.rstrip("/") == key.rstrip("/") or (key.endswith("*") and url.startswith(key[:-1])):
                return FakeResponse(url, text)
        raise FetchError("http_404", url)

    async def get_json(self, url):
        return (await self.get(url)).json()


@pytest.fixture
def afterquery_client(load_fixture):
    """The saved AfterQuery pages (trimmed real responses), keyed by URL."""
    return FakeClient({
        "https://afterquery.com": load_fixture("research_home.html"),
        "https://afterquery.com/contact": load_fixture("research_contact.html"),
        "https://www.ycombinator.com/companies/afterquery": load_fixture("yc_company_page.html"),
        "https://hn.algolia.com/api/v1/search*": load_fixture("hn_search_company.json"),
    })


@pytest.fixture
def afterquery_db():
    """In-memory DB with one company (AfterQuery, as YC lists it) and its HN 'Who is hiring?' post."""
    from sort_sherr.models import Company, JobDraft
    from sort_sherr.storage import db

    conn = db.connect(":memory:")
    cid = db.upsert_company(conn, Company(
        name="AfterQuery", website="https://afterquery.com", domain="afterquery.com", sources=["yc"],
        yc_slug="afterquery", yc_batch="Winter 2025", contact="jobs@afterquery.com",
        description="Applied research lab curating data solutions for foundation models",
    ))
    db.save_jobs(conn, cid, 0, [JobDraft(
        title="Software Engineer", description="AfterQuery (YC W25) | SWE | SF | We build expert training data. Email jobs@afterquery.com",
        apply_url="https://news.ycombinator.com/item?id=46000001",
    )])
    return conn


# ---------- Part 4 helpers ----------


@pytest.fixture
def outreach_db(tmp_path, monkeypatch, profile):
    """Temp-file DB (so CLI commands see it) with one company, a candidate, two drafts (A/B), the
    founder's contact and a fresh saved brief (so a follow-up never fetches anything)."""
    from sort_sherr import config, outreach_config
    from sort_sherr.models import Company
    from sort_sherr.outreach.models import CompanyBrief, Fact
    from sort_sherr.storage import db

    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(outreach_config, "DRAFTS_DIR", tmp_path / "drafts")
    conn = db.connect(path)
    company = db.upsert_company(conn, Company(name="Acme Labs", domain="acme.dev", yc_batch="Winter 2025", team_size=8))
    cand = db.save_candidate(conn, "h", profile.model_dump_json(), json.dumps({"name": "Priya Raman"}), "fake")
    fact = {"id": "F1", "section": "what_they_build", "text": "Turns invoices into JSON", "source_url": "https://acme.dev", "date": None}
    drafts = [db.save_draft(conn, {"match_id": None, "candidate_id": cand, "company_id": company, "variant": v,
                                   "style": s, "subject": "Invoices to JSON and PaperPal", "body": f"Hi Ravi,\n\nbody {v}",
                                   "facts_used": [fact], "student_items_used": [], "check_status": "ok",
                                   "check_notes": [], "lint_flags": []})
              for v, s in (("A", "product-led"), ("B", "project-led"))]
    db.save_contacts(conn, company, [{"type": "email", "value": "ravi@acme.dev", "person_name": "Ravi Kumar",
                                      "person_role": "Co-founder & CTO", "source_type": "first_party",
                                      "source_url": "https://acme.dev/team", "confidence": "high", "mx_ok": 1}])
    brief = CompanyBrief(
        what_they_build=[Fact(text="Turns invoices into JSON", source_url="https://acme.dev")],
        recent_updates=[Fact(text="Launched an open-source Python SDK", date="Nov 2026", source_url="https://acme.dev/blog")],
    )
    db.save_brief(conn, company, brief.model_dump_json(), "[]", "fake")
    return {"conn": conn, "cand": cand, "company": company, "drafts": drafts, "path": path, "tmp": tmp_path}
