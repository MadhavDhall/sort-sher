"""Rippling ATS public job board API (the same JSON their hosted job board page uses)."""

from ...models import JobDraft, guess_is_internship
from ..text import html_to_text

# Rippling's workplaceType -> our `remote` field. Anything else (or missing) = unknown.
WORKPLACE_REMOTE = {"REMOTE": True, "ON_SITE": False, "HYBRID": False}


def api_url(slug: str) -> str:
    return f"https://ats.rippling.com/api/v2/board/{slug}/jobs?pageSize=100"


def parse(data: dict) -> list[JobDraft]:
    jobs = []
    for j in data.get("items", []):
        title = j["name"].strip()
        locations = j.get("locations") or []
        workplace = {loc.get("workplaceType") for loc in locations}
        jobs.append(
            JobDraft(
                title=title,
                team=(j.get("department") or {}).get("name"),
                location="; ".join(loc["name"] for loc in locations if loc.get("name")) or None,
                # remote only if EVERY listed location is remote
                remote=WORKPLACE_REMOTE.get(workplace.pop()) if len(workplace) == 1 else None,
                is_internship=guess_is_internship(title),
                description=html_to_text(j.get("description")),
                apply_url=j.get("url"),
            )
        )
    return jobs
