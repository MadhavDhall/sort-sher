"""Ashby public Job Posting API. Docs: https://developers.ashbyhq.com/docs/public-job-posting-api"""

from ...models import JobDraft, guess_is_internship
from ..text import html_to_text, parse_date


WORKPLACE_REMOTE = {"Remote": True, "Hybrid": False, "OnSite": False}


def api_url(slug: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"


def parse(data: dict) -> list[JobDraft]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        title = j["title"].strip()
        employment_type = j.get("employmentType")  # "FullTime", "Intern", "Contract", ...
        jobs.append(
            JobDraft(
                title=title,
                team=j.get("team") or j.get("department"),
                location=j.get("location"),
                # Careful: Ashby's `isRemote` is ALSO true for hybrid jobs. `workplaceType`
                # (Remote / Hybrid / OnSite) is the accurate field; isRemote is only a fallback.
                remote=WORKPLACE_REMOTE.get(j.get("workplaceType"), j.get("isRemote")),
                is_internship=employment_type == "Intern" or guess_is_internship(title),
                employment_type=employment_type,
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                apply_url=j.get("jobUrl") or j.get("applyUrl"),
                posted_at=parse_date(j.get("publishedAt")),
            )
        )
    return jobs
