"""Lever public Postings API. Docs: https://github.com/lever/postings-api"""

from ...models import JobDraft, guess_is_internship, guess_remote
from ..text import html_to_text, parse_date


WORKPLACE_REMOTE = {"remote": True, "onsite": False, "hybrid": False}  # anything else -> guess from location


def api_url(slug: str) -> str:
    return f"https://api.lever.co/v0/postings/{slug}?mode=json"


def parse(data) -> list[JobDraft]:
    # An unknown board returns {"ok": false, "error": "Document not found"} instead of a list.
    if isinstance(data, dict):
        return []
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        title = j["text"].strip()
        commitment = cats.get("commitment")
        # "lists" are sections like "What you'll do" / "Requirements" -- join them into requirements.
        sections = [f"{s.get('text', '')}\n{html_to_text(s.get('content')) or ''}" for s in j.get("lists") or []]
        workplace = j.get("workplaceType")  # "remote" / "hybrid" / "onsite" / "unspecified"
        jobs.append(
            JobDraft(
                title=title,
                team=cats.get("team") or cats.get("department"),
                location=cats.get("location"),
                remote=WORKPLACE_REMOTE.get(workplace, guess_remote(cats.get("location"))),
                is_internship=guess_is_internship(title, commitment),
                employment_type=commitment,
                description=j.get("descriptionPlain") or html_to_text(j.get("description")),
                requirements="\n\n".join(sections).strip() or None,
                apply_url=j.get("hostedUrl") or j.get("applyUrl"),
                posted_at=parse_date(j.get("createdAt")),
            )
        )
    return jobs
