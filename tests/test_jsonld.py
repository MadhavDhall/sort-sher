from sort_sherr.extractors import jsonld

PAGE = "https://acme.com/careers"


def test_extracts_real_jobposting(load_fixture):
    jobs = jsonld.extract_from_html(load_fixture("jsonld_job.html"), PAGE)
    assert len(jobs) == 1  # Organization block ignored, broken block skipped

    job = jobs[0]
    assert job.title == "Full Stack Engineer"
    assert job.location == "San Francisco, California, US"
    assert job.employment_type == "Full-time"  # "FULL_TIME" normalized
    assert job.is_internship is False
    assert job.remote is None  # not stated -> unknown, not False
    assert job.apply_url == "https://www.ycombinator.com/companies/mem0/jobs/i1LyerD-full-stack-engineer"
    assert job.posted_at.year == 2026
    assert "memory layer" in job.description and "<p>" not in job.description


def test_graph_intern_remote(load_fixture):
    [job] = jsonld.extract_from_html(load_fixture("jsonld_graph.html"), PAGE)
    assert job.title == "Software Engineering Intern (Summer 2027)"
    assert job.employment_type == "Internship"  # "INTERN, TEMPORARY" normalized
    assert job.is_internship is True
    assert job.remote is True  # TELECOMMUTE
    assert job.requirements == "Comfortable with Python or TypeScript."
    assert job.apply_url == PAGE  # no url in the posting -> the page we found it on


def test_child_links(load_fixture):
    links = jsonld.child_links(load_fixture("jsonld_graph.html"), PAGE)
    assert links == ["https://acme.com/careers/swe-intern", "https://acme.com/careers/designer"]


def test_no_child_links_when_careers_page_is_homepage(load_fixture):
    assert jsonld.child_links(load_fixture("jsonld_graph.html"), "https://acme.com/") == []


def test_page_without_jsonld():
    assert jsonld.extract_from_html("<html><body>We're hiring!</body></html>", PAGE) == []
