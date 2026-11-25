"""HN 'Who is hiring?' parsing, on real posts saved from the November 2026 thread."""

from sort_sherr.models import AtsType
from sort_sherr.sources import hn


def posts_by_id(load_fixture):
    return {p.hn_id: p for p in hn.parse_thread(load_fixture("hn_thread.json"))}


def test_standard_header(load_fixture):
    p = posts_by_id(load_fixture)[50338736]
    assert p.company == "Synteq"
    assert p.roles == ["Growth Lead & Systems Administrator"]
    assert p.location == "Austin, TX"
    assert p.remote is True  # "REMOTE (US)"
    assert p.employment_type == "Full-time"


def test_parenthetical_removed_and_onsite(load_fixture):
    p = posts_by_id(load_fixture)[50338792]  # "Shepherd (Series B) | ONSITE | San Francisco, CA"
    assert p.company == "Shepherd"
    assert p.remote is False
    assert p.location == "San Francisco, CA"


def test_header_after_update_note(load_fixture):
    p = posts_by_id(load_fixture)[50338809]  # first paragraph is "UPDATE (Nov 13): ..."
    assert p.company == "Bridge AI Consulting"


def test_contact_and_ats_and_internship(load_fixture):
    posts = posts_by_id(load_fixture)
    assert posts[50338775].contact == "jobs@openeducation.foundation"
    united = posts[50338822]
    assert (united.ats_type, united.ats_slug) == (AtsType.lever, "auw")
    assert united.mentions_internship is True


def test_to_company_splits_homepage_and_careers_url(load_fixture):
    c = hn.to_company(posts_by_id(load_fixture)[50338732])
    assert c.website == "https://www.simpleanalytics.com"
    assert c.domain == "simpleanalytics.com"
    assert c.careers_url == "https://www.simpleanalytics.com/jobs/devops"
    assert c.sources == ["hn"] and c.is_hiring is True


def test_to_job_points_at_hn_post(load_fixture):
    job = hn.to_job(posts_by_id(load_fixture)[50338736])
    assert job.apply_url == "https://news.ycombinator.com/item?id=50338736"
    assert job.is_internship is False  # roles are listed and none is an internship


def test_email_deobfuscation():
    assert hn.find_email("write to jobs [at] acme [dot] io please") == "jobs@acme.io"


def test_post_without_header_is_skipped():
    assert hn.parse_post({"id": 1, "text": "Great thread, thanks!"}) is None
