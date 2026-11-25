from sort_sherr.extractors.ats import gem


def test_parses_jobs(load_fixture):
    jobs = gem.parse(load_fixture("gem.json"))  # real response from Rootly's board
    assert len(jobs) == 2

    job = jobs[0]
    assert job.title == "Senior Product Engineer"
    assert job.team == "Engineering"
    assert job.location == "Toronto, Canada"
    assert job.remote is False  # location_type "hybrid"
    assert job.employment_type == "Full-time"  # "full_time" normalized
    assert job.is_internship is False
    assert job.apply_url == "https://jobs.gem.com/rootly/4015886007"
    assert job.posted_at.year == 2023


def test_intern_employment_type():
    [job] = gem.parse([{"title": "Software Engineer", "employment_type": "intern"}])
    assert job.is_internship is True
