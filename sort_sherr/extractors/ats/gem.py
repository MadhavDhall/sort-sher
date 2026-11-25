"""Gem ATS public job board API (shape is very similar to Greenhouse's)."""

from ...models import JobDraft, guess_is_internship
from ..text import html_to_text, parse_date

LOCATION_REMOTE = {"remote": True, "in_office": False, "onsite": False, "hybrid": False}


def api_url(slug: str) -> str:
    return f"https://api.gem.com/job_board/v0/{slug}/job_posts/"


def parse(data: list) -> list[JobDraft]:
    jobs = []
    for j in data:
        title = j["title"].strip()
        departments = [d["name"] for d in j.get("departments") or []]
        employment_type = j.get("employment_type")  # "full_time", "intern", ...
        jobs.append(
            JobDraft(
                title=title,
                team=departments[0] if departments else None,
                location=(j.get("location") or {}).get("name"),
                remote=LOCATION_REMOTE.get(j.get("location_type")),
                is_internship=guess_is_internship(title, employment_type),
                employment_type=employment_type,
                description=j.get("content_plain") or html_to_text(j.get("content")),
                apply_url=j.get("absolute_url"),
                posted_at=parse_date(j.get("first_published_at") or j.get("created_at")),
            )
        )
    return jobs
