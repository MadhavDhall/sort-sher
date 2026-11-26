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
