"""Dover ATS. Needs TWO requests:
  1. /careers-page-slug/{slug}  -> the company's internal id
  2. /careers-page/{id}/jobs    -> the open jobs
(This is the same JSON Dover's own job board page loads in the browser.)
"""

from ...http import PoliteClient
from ...models import JobDraft, guess_is_internship

BASE = "https://app.dover.com/api/v1"
WORKPLACE_REMOTE = {"REMOTE": True, "IN_OFFICE": False, "ONSITE": False, "HYBRID": False}


async def fetch(client: PoliteClient, slug: str) -> list[JobDraft]:
    page = await client.get_json(f"{BASE}/careers-page-slug/{slug}")
    jobs = await client.get_json(f"{BASE}/careers-page/{page['id']}/jobs")
    return parse(jobs, slug)


def parse(data: dict, slug: str) -> list[JobDraft]:
    jobs = []
    for j in data.get("results", []):
        if not j.get("is_published", True) or j.get("is_sample"):
            continue  # unpublished or Dover's demo jobs
        title = j["title"].strip()
        names = [loc.get("name") for loc in j.get("locations") or [] if loc.get("name")]
        jobs.append(
            JobDraft(
                title=title,
                location="; ".join(names) or None,
                remote=WORKPLACE_REMOTE.get(j.get("workplace_type")),
                is_internship=guess_is_internship(title),
                apply_url=f"https://jobs.dover.com/{slug}/{j['id']}",
            )
        )
    return jobs
