"""Tier 3 with a fake LLM provider: no network, no API key, no cost."""

import asyncio

from sort_sherr.extractors import llm
from sort_sherr.storage import db

PAGE = "https://acme.com/careers"
HTML = """<html><body><h1>Careers at Acme</h1>
<p>We are a small team building tools for farmers. """ + ("We love soil. " * 30) + """</p>
<h2>Open roles</h2>
<ul><li><a href="/careers/swe-intern">Software Engineering Intern</a> - Bangalore</li>
<li><a href="/careers/founding-eng">Founding Engineer</a> - Remote</li></ul>
</body></html>"""


class FakeProvider:
    def __init__(self):
        self.calls = 0

    async def extract_jobs(self, markdown, page_url):
        self.calls += 1
        assert "Software Engineering Intern" in markdown  # the job list survived HTML -> markdown
        return [
            llm.LLMJob(title="Software Engineering Intern", location="Bangalore", apply_url="https://acme.com/careers/swe-intern"),
            llm.LLMJob(title="Founding Engineer", location="Remote", remote=True),
        ]


def test_llm_extract_and_cache():
    conn = db.connect(":memory:")
    provider = FakeProvider()

    jobs, notes = asyncio.run(llm.extract_jobs(conn, provider, HTML, PAGE))
    assert [j.title for j in jobs] == ["Software Engineering Intern", "Founding Engineer"]
    assert jobs[0].is_internship is True  # filled in by our keyword check when the LLM left it null
    assert jobs[1].is_internship is None  # unknown stays unknown
    assert jobs[1].remote is True

    # Same page again -> served from cache, the LLM is not called a second time.
    jobs2, notes2 = asyncio.run(llm.extract_jobs(conn, provider, HTML, PAGE))
    assert provider.calls == 1
    assert "llm cache hit" in notes2
    assert [j.title for j in jobs2] == [j.title for j in jobs]

    # Page changes -> cache miss -> LLM runs again.
    asyncio.run(llm.extract_jobs(conn, provider, HTML.replace("farmers", "fishers"), PAGE))
    assert provider.calls == 2


# ---------- GeminiProvider, with the network call replaced by a fake ----------


class FakeResponse:
    def __init__(self, parsed=None, text=""):
        self.parsed, self.text = parsed, text


def make_gemini(monkeypatch, answers):
    """A real GeminiProvider whose generate_content returns/raises `answers` in order."""
    from sort_sherr import config

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(llm.asyncio, "sleep", fake_sleep)  # don't really wait in tests
    monkeypatch.setattr(config, "LLM_MIN_INTERVAL", 0)
    provider = llm.GeminiProvider(api_key="test-key")
    calls = []

    async def fake_generate_content(**kwargs):
        calls.append(kwargs)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(provider.client.aio.models, "generate_content", fake_generate_content)
    return provider, calls, sleeps


def test_gemini_returns_parsed_jobs(monkeypatch):
    parsed = llm.LLMJobList(jobs=[llm.LLMJob(title="Backend Intern")])
    provider, calls, _ = make_gemini(monkeypatch, [FakeResponse(parsed=parsed)])

    jobs = asyncio.run(provider.extract_jobs("some page", PAGE))
    assert [j.title for j in jobs] == ["Backend Intern"]
    assert calls[0]["config"].response_schema is llm.LLMJobList  # we asked for our schema
    assert PAGE in calls[0]["contents"]


def test_gemini_falls_back_to_raw_json_text(monkeypatch):
    provider, _, _ = make_gemini(monkeypatch, [FakeResponse(parsed=None, text='{"jobs": [{"title": "Designer"}]}')])
    assert [j.title for j in asyncio.run(provider.extract_jobs("page", PAGE))] == ["Designer"]


def test_gemini_waits_and_retries_on_rate_limit(monkeypatch):
    from google.genai import errors

    too_many = errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    ok = FakeResponse(parsed=llm.LLMJobList(jobs=[]))
    provider, calls, sleeps = make_gemini(monkeypatch, [too_many, ok])

    assert asyncio.run(provider.extract_jobs("page", PAGE)) == []
    assert len(calls) == 2  # tried again after the 429
    assert 30 in sleeps  # and waited 30 s before retrying


def test_markdown_drops_scripts_and_styles():
    html = "<html><head><style>body{color:red}</style></head><body><script>var x=1</script><p>Hello jobs</p></body></html>"
    md = llm.html_to_markdown(html)
    assert "Hello jobs" in md
    assert "color:red" not in md and "var x" not in md
