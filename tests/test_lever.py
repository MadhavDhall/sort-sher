from sort_sherr.extractors.ats import lever


def test_parses_jobs(load_fixture):
    jobs = lever.parse(load_fixture("lever.json"))
    assert len(jobs) == 2

    job = jobs[0]
    assert job.title == "Account Executive - Commercial, Spotify Technology"
    assert job.team == "Platform"
    assert job.location == "New York, NY"
    assert job.employment_type == "Full-time"  # "Permanent" normalized
    assert job.remote is False  # workplaceType "hybrid"
    assert job.is_internship is False
    assert job.apply_url.startswith("https://jobs.lever.co/spotify/")
    assert job.posted_at.year == 2026  # createdAt is epoch milliseconds
    assert job.requirements  # built from the "lists" sections


def test_unknown_board_returns_no_jobs():
    assert lever.parse({"ok": False, "error": "Document not found"}) == []


def test_intern_commitment_is_internship():
    raw = [{"text": "Software Engineer", "categories": {"commitment": "Intern"}, "hostedUrl": "https://x"}]
    assert lever.parse(raw)[0].is_internship is True
