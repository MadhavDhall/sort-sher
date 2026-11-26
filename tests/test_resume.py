"""Step A: resume parsing, with a fake LLM. The fixtures are a made-up student (see make_resume_fixtures.py)."""

import asyncio
import json
from pathlib import Path

import pytest

from sort_sherr.matching import resume
from sort_sherr.matching.models import Education, ParsedResume, Project
from sort_sherr.storage import db

FIXTURES = Path(__file__).parent / "fixtures"
PII = ["Priya", "Raman", "priya.raman@example.com", "98765", "github.com/priya-fake", "paperpal-demo.dev", "linkedin.com/in"]


class FakeProvider:
    """Records the prompt it got and answers like Gemini would."""

    model = "fake-model"

    def __init__(self):
        self.prompts = []

    async def generate(self, prompt, schema):
        self.prompts.append(prompt)
        parsed = ParsedResume(
            education=[Education(college="IIT Roorkee", degree="B.Tech", branch="Computer Science", grad_date="2026")],
            skills=["Python", "ReactJS", "Node", "Postgres", "PyTorch", "LangChain", "Docker", "python3"],
            projects=[
                Project(title="PaperPal", tech_stack=["LangChain", "FAISS"], links=["[LINK_2]"], impact="300 weekly users")
            ],
            focus_areas=["GenAI", "backend"],
        )
        return parsed, {"prompt_tokens": 500, "output_tokens": 200}


@pytest.mark.parametrize("name", ["resume_fake.pdf", "resume_fake.docx"])
def test_extract_text_and_hidden_links(name):
    text, links = resume.extract_text(FIXTURES / name)
    assert "PaperPal" in text and "IIT" not in links
    assert links == ["https://www.linkedin.com/in/priya-fake"]  # the link hidden behind the word "LinkedIn"


def test_redact_removes_personal_details_but_keeps_dates():
    text, _ = resume.extract_text(FIXTURES / "resume_fake.pdf")
    redacted, pii = resume.redact(text)
    assert pii["name"] == "Priya Raman"
    assert pii["emails"] == ["priya.raman@example.com"]
    assert pii["phones"] == ["+91 98765 43210"]
    assert pii["links"] == ["github.com/priya-fake", "www.paperpal-demo.dev"]
    for secret in ["Priya", "Raman", "example.com", "98765", "priya-fake"]:
        assert secret not in redacted
    assert "2022 - 2026" in redacted  # a date range is not a phone number
    assert "[LINK_2]" in redacted


def test_name_found_mid_page_via_email_and_ids_removed():
    # College templates often start with a heading and put the name further down.
    text = "Area of Interest\nMachine learning, Backend\nEducation\nAsha Verma\nEmail: asha_v@college.ac.in\nRegistration No: 24124023/2027"
    redacted, pii = resume.redact(text)
    assert pii["name"] == "Asha Verma"
    assert "Asha" not in redacted and "Verma" not in redacted and "24124023" not in redacted
    assert "Area of Interest" in redacted


@pytest.mark.parametrize("text, links", [
    ("Skills\nPython\nKaran Mehta\nkmehta99@gmail.com", []),  # surname inside the email username
    ("Profile\nGo, Rust\nKaran Mehta\nContact: [see links]", ["https://linkedin.com/in/karan-mehta-01"]),  # URL slug
    ("Karan Mehta\nSoftware engineer", []),  # plain first line
])
def test_find_name_in_different_templates(text, links):
    assert resume.find_name(text, links) == "Karan Mehta"


def test_grad_year_set_by_student(profile):
    assert resume.grad_year(profile) is None
    resume.set_grad_year(profile, 2028)
    assert resume.grad_year(profile) == 2028


def test_cli_asks_name_before_sending_and_grad_year_after(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sort_sherr import cli, config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    fake = FakeProvider()  # answers grad_date "2026"
    monkeypatch.setattr(cli, "need_llm", lambda: fake)
    resume_file = str(FIXTURES / "resume_fake.pdf")

    # The guessed name is offered; the student confirms (Enter) and corrects the year to 2028.
    out = CliRunner().invoke(cli.app, ["parse-resume", resume_file], input="\n2028\n")
    assert out.exit_code == 0, out.output
    assert "[Priya Raman]" in out.output and "[2026]" in out.output
    assert "grad 2028" in out.output
    assert "Priya" not in fake.prompts[0]

    # Same file again: no LLM call, the year can be fixed again and is saved.
    out = CliRunner().invoke(cli.app, ["parse-resume", resume_file, "--grad-year", "2029"])
    assert out.exit_code == 0 and "grad 2029" in out.output and len(fake.prompts) == 1
    stored = db.get_candidate(db.connect(config.DB_PATH), 1)["profile_json"]
    assert '"grad_date":"2029"' in stored


def test_parse_refuses_when_no_name_found(tmp_path):
    # Nothing name-like anywhere -> stop rather than risk sending the name.
    import docx

    d = docx.Document()
    d.add_paragraph("SKILLS: Python, Go, Rust, " + "distributed systems " * 10)
    d.save(tmp_path / "r.docx")
    with pytest.raises(ValueError, match="--name"):
        asyncio.run(resume.parse_resume(db.connect(":memory:"), FakeProvider(), tmp_path / "r.docx"))


def test_normalize_skills():
    assert resume.normalize_skills(["ReactJS", "react.js", "Postgres", "py", "Rust"]) == ["React", "PostgreSQL", "Python", "Rust"]


@pytest.mark.parametrize("name", ["resume_fake.pdf", "resume_fake.docx"])
def test_parse_resume_never_sends_pii_and_caches(name):
    conn = db.connect(":memory:")
    provider = FakeProvider()

    cid, profile, cached = asyncio.run(resume.parse_resume(conn, provider, FIXTURES / name))
    assert not cached
    prompt = provider.prompts[0]
    for secret in PII:
        assert secret.lower() not in prompt.lower(), f"{secret!r} leaked into the LLM prompt"

    # personal bits come back locally
    assert profile.name == "Priya Raman"
    assert "https://www.linkedin.com/in/priya-fake" in profile.links
    assert profile.projects[0].links == ["www.paperpal-demo.dev"]  # [LINK_2] mapped back
    assert profile.skills == ["Python", "React", "Node.js", "PostgreSQL", "PyTorch", "LangChain", "Docker"]

    # saved, with PII kept in its own local column
    row = db.get_candidate(conn, cid)
    assert json.loads(row["pii_json"])["phones"] == ["+91 98765 43210"]
    assert row["model"] == "fake-model"

    # same file again -> no second LLM call
    cid2, profile2, cached2 = asyncio.run(resume.parse_resume(conn, provider, FIXTURES / name))
    assert cached2 and cid2 == cid and len(provider.prompts) == 1
    assert profile2 == profile


def test_unsupported_file_type(tmp_path):
    f = tmp_path / "resume.txt"
    f.write_text("hello")
    with pytest.raises(ValueError):
        resume.extract_text(f)
