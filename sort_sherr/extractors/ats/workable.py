"""Workable public widget API (the same JSON their embeddable job widget uses).

Note: this endpoint has no job descriptions, only the listing.
"""

from ...models import JobDraft, guess_is_internship
from ..text import parse_date


def api_url(slug: str) -> str:
    return f"https://apply.workable.com/api/v1/widget/accounts/{slug}"


def parse(data: dict) -> list[JobDraft]:
    jobs = []
    for j in data.get("jobs", []):
        title = j["title"].strip()
        location = ", ".join(p for p in (j.get("city"), j.get("country")) if p) or None
        employment_type = j.get("employment_type") or None
        jobs.append(
            JobDraft(
                title=title,
                team=j.get("department") or None,
                location=location,
                remote=j.get("telecommuting"),
                is_internship=guess_is_internship(title, employment_type),
                employment_type=employment_type,
                apply_url=j.get("url") or j.get("shortlink"),
                posted_at=parse_date(j.get("published_on") or j.get("created_at")),
            )
        )
    return jobs
