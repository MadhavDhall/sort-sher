from sort_sherr.extractors.ats import ashby


def test_parses_jobs(load_fixture):
    jobs = ashby.parse(load_fixture("ashby.json"))
    assert [j.title for j in jobs] == ["Client Growth Manager", "GTM Associate - Internship"]

    full_time, intern = jobs
    assert full_time.employment_type == "Full-time"  # "FullTime" normalized
    assert full_time.is_internship is False
    assert full_time.remote is False
    assert full_time.location == "San Francisco"
    assert full_time.apply_url.startswith("https://jobs.ashbyhq.com/conveo/")

    assert intern.employment_type == "Internship"
    assert intern.is_internship is True


def test_unlisted_jobs_are_skipped():
    data = {"jobs": [{"title": "Secret role", "isListed": False}]}
    assert ashby.parse(data) == []
