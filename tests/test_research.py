"""Part 3, step A: research a company from saved pages (fake client, fake LLM, no network)."""

import asyncio

from sort_sherr.outreach import research
from sort_sherr.outreach.models import CompanyBrief, Fact, Person, numbered_facts
from sort_sherr.storage import db


class BriefLLM:
    """Returns a fixed brief: some facts cite real pages, some cite pages we never read."""

    model = "fake-llm"

    def __init__(self):
        self.calls, self.prompts = 0, []

    async def generate(self, prompt, schema):
        assert schema is CompanyBrief
        self.calls += 1
        self.prompts.append(prompt)
        return CompanyBrief(
            what_they_build=[Fact(text="Curates expert training data for frontier AI labs", source_url="https://afterquery.com/")],
            recent_updates=[
                Fact(text="Became YC's fastest unicorn at $3.2B", date="Sep 01, 2026",
                     source_url="https://www.forbes.com/sites/annatong/2026/09/01/afterquery-becomes-ycs-fastest-unicorn-at-32-billion/"),
                Fact(text="Raised a made-up Series Z", source_url="https://techcrunch.com/not-a-page-we-read"),  # dropped
                Fact(text="No source at all", source_url=""),  # dropped
            ],
            hiring_areas=[Fact(text="Agent environments across APIs and MCP", source_url="https://afterquery.com#solution")],
            people=[
                Person(name="Carlos Georgescu", role="Founder/CTO", source_url="https://www.ycombinator.com/companies/afterquery"),
                Person(name="Jane Invented", role="CEO", source_url="https://www.ycombinator.com/companies/afterquery"),  # dropped
            ],
        ), {"prompt_tokens": 5000, "output_tokens": 400}


def run(coro):
    return asyncio.run(coro)


def test_yc_page_becomes_text_pages(load_fixture):
    pages = research.yc_pages(load_fixture("yc_company_page.html"), "https://www.ycombinator.com/companies/afterquery")
    main = pages[0]
    assert main.kind == "yc" and "Founder: Carlos Georgescu, Founder/CTO" in main.text
    assert "https://www.linkedin.com/in/carlossgg/" in main.links
    assert "Open role: Software Engineer Intern" in main.text
    assert any(p.url.startswith("https://www.ycombinator.com/launches/") for p in pages)  # Launch YC post
    news = [p for p in pages if p.kind == "news"]
    assert news and "Sep 01, 2026" in news[0].text


def test_pick_site_links_prefers_useful_pages_on_own_domain(load_fixture):
    home = research.page_from_html("https://afterquery.com", "company", load_fixture("research_home.html"))
    links = research.pick_site_links(home, "afterquery.com", 5)
    assert links[0] == "https://afterquery.com/products"  # "product" ranks before careers/contact/research
    assert "https://afterquery.com/careers" in links and "https://afterquery.com/contact" in links
    assert not any("privacy" in l or "terms" in l or "linkedin" in l for l in links)


def test_hn_story_filter():
    hits = {"hits": [
        {"objectID": "1", "title": "Launch HN: Acme (YC W25) - invoices to JSON", "created_at": "2026-03-01"},
        {"objectID": "2", "title": "Show HN: a toy", "url": "https://acme.dev/toy", "created_at": "2026-02-01"},
        {"objectID": "3", "title": "Acme Corp recalls rockets", "url": "https://news.example/acme", "created_at": "2026-01-01"},
    ]}
    pages = research.hn_pages(hits, "Acme", "acme.dev")
    assert [p.url[-1] for p in pages] == ["1", "2"]  # a short common name in a news title is not enough
    assert all(p.kind == "hn_launch" for p in pages)


def test_research_drops_unsourced_facts_and_caches(afterquery_db, afterquery_client):
    llm = BriefLLM()
    cid = 1
    out = run(research.research_company(afterquery_db, afterquery_client, llm, cid))
    brief = out["brief"]
    assert out["dropped"] == 3  # the made-up page, the empty source and the invented person
    assert [f.text for f in brief.recent_updates] == ["Became YC's fastest unicorn at $3.2B"]
    assert brief.hiring_areas[0].source_url == "https://afterquery.com"  # normalized to the page we read
    assert [p.name for p in brief.people] == ["Carlos Georgescu"]
    assert all(f["source_url"] for f in numbered_facts(brief))

    kinds = {p.kind for p in out["pages"]}
    assert {"company", "yc", "news", "hn", "hn_hiring"} <= kinds
    assert "We build expert training data" in llm.prompts[0]  # their HN hiring post was read
    assert not any("linkedin.com" in u or "x.com" in u for u in afterquery_client.fetched)

    again = run(research.research_company(afterquery_db, afterquery_client, llm, cid))
    assert again["cached"] and llm.calls == 1  # fresh brief -> no second LLM call
    run(research.research_company(afterquery_db, afterquery_client, llm, cid, refresh=True))
    assert llm.calls == 2


def test_stale_brief_is_rebuilt(afterquery_db, afterquery_client):
    llm = BriefLLM()
    run(research.research_company(afterquery_db, afterquery_client, llm, 1))
    afterquery_db.execute("UPDATE company_briefs SET fetched_at = '2020-01-01T00:00:00+00:00'")
    run(research.research_company(afterquery_db, afterquery_client, llm, 1))
    assert llm.calls == 2
    assert db.get_brief(afterquery_db, 1)["fetched_at"] > "2020"
