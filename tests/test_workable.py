from sort_sherr.extractors.ats import workable


def test_parses_jobs(load_fixture):
    jobs = workable.parse(load_fixture("workable.json"))
    assert len(jobs) == 2

    job = jobs[0]
    assert job.title == "Open-Source Machine Learning Engineer - EMEA Remote"
    assert job.team == "Open Source"
    assert job.location == "Paris, France"
    assert job.remote is True  # "telecommuting": true
    assert job.employment_type == "Full-time"
    assert job.is_internship is False
    assert job.apply_url == "https://apply.workable.com/j/81B46579FE"
    assert job.description is None  # the widget API has no descriptions
