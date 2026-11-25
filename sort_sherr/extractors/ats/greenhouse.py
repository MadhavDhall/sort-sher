"""Greenhouse public Job Board API. Docs: https://developers.greenhouse.io/job-board.html"""

from ...models import JobDraft, guess_is_internship, guess_remote
from ..text import html_to_text, parse_date


def api_url(slug: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def parse(data: dict) -> list[JobDraft]:
    jobs = []
    for j in data.get("jobs", []):
        title = j["title"].strip()
        location = (j.get("location") or {}).get("name")
        departments = [d["name"] for d in j.get("departments") or []]
        jobs.append(
            JobDraft(
                title=title,
                team=departments[0] if departments else None,
                location=location,
                remote=guess_remote(location, title),
                is_internship=guess_is_internship(title),
                description=html_to_text(j.get("content")),
                apply_url=j.get("absolute_url"),
                posted_at=parse_date(j.get("first_published") or j.get("updated_at")),
            )
        )
    return jobs
