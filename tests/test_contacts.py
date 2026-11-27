"""Part 3, step B: contacts. Saved pages, fake client, stubbed MX lookups (no network, no DNS)."""

import asyncio
import json

import pytest

from conftest import FakeClient
from sort_sherr.models import Company
from sort_sherr.outreach import contacts as ct
from sort_sherr.outreach import research
from sort_sherr.outreach.models import CompanyBrief, Person
from sort_sherr.storage import db

from test_research import BriefLLM


async def mx_yes(domain):
    return True


async def mx_no_for_dead(domain):
    return domain != "dead-startup.io"


def run(coro):
    return asyncio.run(coro)


# A team page in the shape small startups use: name + role + icon links on one line.
TEAM_PAGE = """<html><body>
<h1>Team</h1>
<p>Ravi Kumar, Co-founder &amp; CTO <a href="https://www.linkedin.com/in/ravi-kumar-acme">LinkedIn</a>
   <a href="https://github.com/octocat">GitHub</a></p>
<p>Write to the founders: <a href="mailto:acmefounders@gmail.com">acmefounders@gmail.com</a></p>
<p>Careers: <a href="mailto:jobs@acme.dev">jobs@acme.dev</a> &middot; Office <a href="tel:+1-415-555-0100">call us</a></p>
<p><a href="https://x.com/acme">Acme on X</a> <img src="logo@2x.png"></p>
</body></html>"""


@pytest.fixture
def acme():
    conn = db.connect(":memory:")
    cid = db.upsert_company(conn, Company(name="Acme", website="https://acme.dev", domain="acme.dev",
                                          sources=["hn"], careers_url="https://acme.dev/careers"))
    page = research.page_from_html("https://acme.dev/team", "company", TEAM_PAGE)
    brief = CompanyBrief(what_they_build=[], people=[Person(name="Ravi Kumar", role="Co-founder & CTO", source_url=page.url)])
    return conn, cid, page, brief


def test_first_party_rules(acme):
    conn, cid, page, brief = acme
    found = ct.first_party_contacts(cid, "acme.dev", [page], ct.people_on_pages(brief, [page]))
    by_value = {c.value: c for c in found}
    # an off-domain email the company published itself is KEPT, but marked and less certain
    gmail = by_value["acmefounders@gmail.com"]
    assert gmail.confidence == "medium" and gmail.evidence.startswith("not on the company domain")
    assert by_value["jobs@acme.dev"].confidence == "high"
    assert by_value["+1-415-555-0100"].type == "phone"  # official phone from their own site
    linkedin = by_value["https://www.linkedin.com/in/ravi-kumar-acme"]
    assert linkedin.person_name == "Ravi Kumar" and linkedin.source_url == "https://acme.dev/team"
    assert "https://x.com/acme" not in by_value  # the company's own X account is not a person
    assert not any("logo" in v for v in by_value)  # image file names aren't emails


def test_phone_only_from_company_site():
    page = research.page_from_html("https://news.ycombinator.com/item?id=1", "hn_hiring", '<a href="tel:+15550000">x</a>')
    assert not [c for c in ct.first_party_contacts(1, "acme.dev", [page], []) if c.type == "phone"]


def test_off_domain_email_from_third_party_is_dropped(acme, load_fixture):
    conn, cid, page, brief = acme
    user = load_fixture("github_user.json")  # real API shape (GitHub's demo user)
    work = dict(user, email="ravi@acme.dev", twitter_username="ravi_builds")
    personal = dict(user, email="ravi.kumar@gmail.com")
    first = ct.first_party_contacts(cid, "acme.dev", [page], ct.people_on_pages(brief, [page]))

    got = run(ct.github_contacts(FakeClient({"https://api.github.com/users/octocat": json.dumps(work)}), cid, "acme.dev", first))
    assert {(c.type, c.value) for c in got} >= {("email", "ravi@acme.dev"), ("x", "https://x.com/ravi_builds")}
    assert all(c.source_type == "third_party" and c.source_url == "https://github.com/octocat" for c in got)

    got = run(ct.github_contacts(FakeClient({"https://api.github.com/users/octocat": json.dumps(personal)}), cid, "acme.dev", first))
    assert not [c for c in got if c.type == "email"]  # gmail found only on GitHub = personal -> not collected


def test_hunter_parse_and_guess(load_fixture):
    data = load_fixture("hunter_domain_search_docs_example.json")["data"]
    people = [{"name": "Meera Shah", "role": "CEO"}]
    got = {c.value: c for c in ct.parse_hunter(data, 1, "acme.dev", people)}
    assert got["ravi@acme.dev"].verified and got["ravi@acme.dev"].source_url == "https://blog.acme.dev/hello-world"
    assert "nosource@acme.dev" not in got  # no source page -> dropped
    assert "ravi.k@gmail.com" not in got  # off-domain from an API -> dropped
    guess = got["meera@acme.dev"]
    assert guess.is_guess and guess.confidence == "low" and "UNVERIFIED GUESS" in guess.evidence
    assert "api_key" not in guess.source_url


def test_ranking_follows_the_rules():
    def c(**kw):
        base = dict(type="email", person_name=None, person_role=None, source_type="first_party",
                    confidence="high", mx_ok=1, is_guess=0, verified=0, evidence=None)
        return {**base, **kw}

    contacts = [
        c(type="apply_url", value="https://acme.dev/careers"),
        c(type="linkedin", value="https://linkedin.com/in/ravi", person_name="Ravi", person_role="CTO"),
        c(value="sales@acme.dev"),  # wrong inbox: below social DMs
        c(value="ravi@acme.dev", source_type="api", verified=1, person_name="Ravi"),
        c(value="meera@acme.dev", source_type="api", is_guess=1, confidence="low"),  # never the default
        c(value="team@dead-startup.io", mx_ok=0),  # no mail server: below social DMs
        c(value="hello@acme.dev"),
        c(value="ravi.listed@acme.dev", source_type="third_party", confidence="medium", person_name="Ravi"),
        c(value="acmefounders@gmail.com", confidence="medium", evidence="not on the company domain (acme.dev); ..."),
        c(value="jobs@acme.dev"),
        c(value="research@acme.dev"),  # an inbox we can't place: below the founder's DM
    ]
    order = [x["value"] for x in ct.rank_contacts(contacts)]
    assert order == [
        "jobs@acme.dev", "hello@acme.dev", "acmefounders@gmail.com",  # first-party (own domain before off-domain)
        "ravi@acme.dev",  # API-verified
        "ravi.listed@acme.dev",  # third-party listed
        "https://linkedin.com/in/ravi",  # social DM
        "research@acme.dev",
        "sales@acme.dev", "team@dead-startup.io",
        "https://acme.dev/careers",  # apply URL
        "meera@acme.dev",  # guess: last
    ]
    assert ct.default_recipient([contacts[4], contacts[0]])["value"] == "https://acme.dev/careers"


def test_find_contacts_end_to_end(afterquery_db, afterquery_client):
    out = run(research.research_company(afterquery_db, afterquery_client, BriefLLM(), 1))
    ranked = run(ct.find_contacts(afterquery_db, afterquery_client, 1, out["pages"], out["brief"], mx_check=mx_yes))
    best = ct.default_recipient(ranked)
    assert best["value"] == "jobs@afterquery.com"  # from their own HN hiring post
    assert best["source_url"] == "https://news.ycombinator.com/item?id=46000001"
    values = [c["value"] for c in ranked]
    assert "https://www.linkedin.com/in/carlossgg" in values  # founder link from the YC page
    assert values.index("https://www.linkedin.com/in/carlossgg") < values.index("sales@afterquery.com")
    assert all(c["source_url"] for c in ranked)
    assert all(c["mx_ok"] == 1 for c in ranked if c["type"] == "email")
    assert not any("linkedin.com" in u or "x.com" in u for u in afterquery_client.fetched)  # linked, never fetched


def test_forget_contact_sticks(afterquery_db, afterquery_client):
    out = run(research.research_company(afterquery_db, afterquery_client, BriefLLM(), 1))
    run(ct.find_contacts(afterquery_db, afterquery_client, 1, out["pages"], out["brief"], mx_check=mx_yes))
    jobs = next(c for c in db.company_contacts(afterquery_db, 1) if c["value"] == "jobs@afterquery.com")
    assert db.forget_contacts(afterquery_db, 1, jobs["id"]) == 1
    run(ct.find_contacts(afterquery_db, afterquery_client, 1, out["pages"], out["brief"], mx_check=mx_yes))
    assert "jobs@afterquery.com" not in [c["value"] for c in db.company_contacts(afterquery_db, 1)]
    assert db.forget_contacts(afterquery_db, 1) > 0 and db.company_contacts(afterquery_db, 1) == []


def test_contact_without_source_is_never_saved():
    conn = db.connect(":memory:")
    cid = db.upsert_company(conn, Company(name="Acme", domain="acme.dev"))
    saved = db.save_contacts(conn, cid, [
        {"type": "email", "value": "a@acme.dev", "source_type": "first_party", "source_url": "", "confidence": "high"},
        {"type": "email", "value": "b@acme.dev", "source_type": "first_party", "source_url": "https://acme.dev", "confidence": "high"},
    ])
    assert saved == 1


# Someone else's HN story about Acme: a work email, a gmail, a phone and Ravi's profile.
THIRD_PARTY_PAGE = """<html><body>
<p>I talked to Ravi Kumar (CTO) about Acme. <a href="https://x.com/ravikumar_dev">@ravikumar_dev</a></p>
<p>They said to write to ravi@acme.dev or ravi.k@gmail.com, or call <a href="tel:+15551234">this</a>.</p>
</body></html>"""


def test_third_party_pages_keep_only_work_emails(acme):
    conn, cid, page, brief = acme
    hn = research.page_from_html("https://news.ycombinator.com/item?id=7", "hn", THIRD_PARTY_PAGE)
    got = {c.value: c for c in ct.third_party_contacts(cid, "acme.dev", [page, hn], ct.people_on_pages(brief, [page]))}
    assert set(got) == {"ravi@acme.dev", "https://x.com/ravikumar_dev"}  # no gmail, no phone, team page skipped
    assert got["ravi@acme.dev"].person_name == "Ravi Kumar"
    assert all(c.source_type == "third_party" and c.source_url == hn.url for c in got.values())


# Ravi's personal site: his work email, a general inbox, a personal gmail, his GitHub and a friend's.
FOUNDER_SITE = """<html><body><h1>Ravi Kumar</h1>
<p>CTO at Acme: <a href="mailto:ravi@acme.dev">ravi@acme.dev</a> (or hello@acme.dev). Personal: ravi.k@gmail.com</p>
<a href="https://github.com/ravikumar">my code</a> <a href="https://github.com/friendbob">Bob's code</a>
</body></html>"""


def test_founder_site_contacts():
    ravi = dict(company_id=1, person_name="Ravi Kumar", person_role="CTO", source_type="third_party",
                source_url="https://github.com/octocat", confidence="medium")
    found = [
        ct.Contact(type="website", value="https://ravi.dev", **ravi),
        ct.Contact(type="website", value="https://acme.dev/blog", **ravi),  # the company site: already read
    ]
    client = FakeClient({"https://ravi.dev": FOUNDER_SITE})
    got = {c.value: c for c in run(ct.founder_site_contacts(client, 1, "acme.dev", found))}
    assert client.fetched == ["https://ravi.dev"]
    assert set(got) == {"ravi@acme.dev", "hello@acme.dev", "https://github.com/ravikumar"}
    assert got["ravi@acme.dev"].person_name == "Ravi Kumar" and got["hello@acme.dev"].person_name is None
    assert all(c.source_type == "first_party" and c.source_url == "https://ravi.dev" for c in got.values())
