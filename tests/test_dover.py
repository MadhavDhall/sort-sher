import asyncio

from sort_sherr.extractors.ats import dover


def test_parses_jobs(load_fixture):
    jobs = dover.parse(load_fixture("dover_jobs.json"), "mixrank")  # real response from MixRank
    titles = [j.title for j in jobs]
    assert "Junior Software Engineer - Remote (Global), Full-Time" in titles

    job = jobs[0]
    assert job.title == "Account Executive - US based/Remote"
    assert job.location == "Canada; United States"
    assert job.remote is True  # workplace_type REMOTE
    assert job.apply_url == "https://jobs.dover.com/mixrank/09a901ed-e730-4e70-a953-d9bf302f6ad2"


def test_sample_and_unpublished_jobs_skipped():
    data = {"results": [
        {"id": "1", "title": "Demo job", "is_sample": True},
        {"id": "2", "title": "Draft job", "is_published": False},
        {"id": "3", "title": "Software Intern", "is_published": True},
    ]}
    jobs = dover.parse(data, "acme")
    assert [j.title for j in jobs] == ["Software Intern"]
    assert jobs[0].is_internship is True


class FakeClient:
    """Answers the two Dover requests from saved files, and records the URLs asked for."""

    def __init__(self, load_fixture):
        self.load_fixture, self.urls = load_fixture, []

    async def get_json(self, url):
        self.urls.append(url)
        return self.load_fixture("dover_page.json" if "careers-page-slug" in url else "dover_jobs.json")


def test_fetch_does_slug_then_jobs(load_fixture):
    client = FakeClient(load_fixture)
    jobs = asyncio.run(dover.fetch(client, "mixrank"))
    assert client.urls == [
        "https://app.dover.com/api/v1/careers-page-slug/mixrank",
        "https://app.dover.com/api/v1/careers-page/1e087dd4-d16e-486d-9b69-f7fddfa2444d/jobs",
    ]
    assert len(jobs) == 3
