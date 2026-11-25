from sort_sherr.extractors.ats import greenhouse


def test_parses_jobs(load_fixture):
    jobs = greenhouse.parse(load_fixture("greenhouse.json"))
    assert len(jobs) == 3

    first = jobs[0]
    assert first.title == "Abuse Investigator"
    assert first.team == "8611 Security Analytics"
    assert first.location == "Seattle, San Francisco, New York City"
    assert first.apply_url == "https://stripe.com/jobs/search?gh_jid=8172510"
    assert first.posted_at.year == 2026
    # content arrives HTML-escaped; we should end up with plain text
    assert first.description and "<" not in first.description and "&lt;" not in first.description


def test_internationalisation_is_not_an_internship(load_fixture):
    jobs = greenhouse.parse(load_fixture("greenhouse.json"))
    i18n = next(j for j in jobs if "Internationalisation" in j.title)
    assert i18n.is_internship is False


def test_api_url():
    assert greenhouse.api_url("stripe") == "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true"
