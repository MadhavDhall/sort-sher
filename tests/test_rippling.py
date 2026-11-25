from sort_sherr.extractors.ats import rippling


def test_parses_jobs(load_fixture):
    jobs = rippling.parse(load_fixture("rippling.json"))  # real response from Saturn's board
    assert len(jobs) == 3

    job = jobs[0]
    assert job.title == "Partner"
    assert job.team == "Partnerships"
    assert job.location == "London, United Kingdom"
    assert job.remote is False  # workplaceType ON_SITE
    assert job.is_internship is False
    assert job.apply_url == "https://ats.rippling.com/saturn/jobs/401593ba-f666-44a4-9c24-107deea0a780"


def test_mixed_workplace_types_are_unknown():
    data = {"items": [{"name": "Engineer", "locations": [
        {"name": "London", "workplaceType": "ON_SITE"},
        {"name": "Anywhere", "workplaceType": "REMOTE"},
    ]}]}
    [job] = rippling.parse(data)
    assert job.remote is None
    assert job.location == "London; Anywhere"
