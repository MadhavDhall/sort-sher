"""Detect which ATS (applicant tracking system) a company uses, and its board slug.

We just search the page HTML (links, iframes, scripts) and the page URL itself
for known ATS URL shapes. One regex per ATS; the slug is the first capture group.
"""

import re

from ..models import AtsType

SLUG = r"([A-Za-z0-9][A-Za-z0-9._-]*)"

ATS_PATTERNS: list[tuple[AtsType, re.Pattern]] = [
    # boards.greenhouse.io/acme, job-boards.greenhouse.io/acme, boards.greenhouse.io/embed/job_board?for=acme
    (AtsType.greenhouse, re.compile(r"greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)" + SLUG, re.I)),
    (AtsType.greenhouse, re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/" + SLUG, re.I)),
    # jobs.lever.co/acme
    (AtsType.lever, re.compile(r"jobs(?:\.eu)?\.lever\.co/" + SLUG, re.I)),
    # jobs.ashbyhq.com/acme
    (AtsType.ashby, re.compile(r"jobs\.ashbyhq\.com/" + SLUG, re.I)),
    # apply.workable.com/acme
    (AtsType.workable, re.compile(r"apply\.workable\.com/" + SLUG, re.I)),
    # ats.rippling.com/acme/jobs, ats.rippling.com/en-GB/acme/jobs (optional language prefix)
    (AtsType.rippling, re.compile(r"ats\.rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?" + SLUG, re.I)),
    # app.dover.com/jobs/acme, app.dover.com/apply/acme/<id>, jobs.dover.com/acme
    (AtsType.dover, re.compile(r"app\.dover\.com/(?:jobs|apply)/" + SLUG, re.I)),
    (AtsType.dover, re.compile(r"jobs\.dover\.com/" + SLUG, re.I)),
    # jobs.gem.com/acme
    (AtsType.gem, re.compile(r"jobs\.gem\.com/" + SLUG, re.I)),
]

# Path segments that look like slugs but aren't company boards.
NOT_A_SLUG = {"embed", "api", "js", "v1", "j", "jobs", "careers", "static", "assets", "_next"}


def detect_ats(html: str, url: str = "") -> tuple[AtsType, str] | None:
    """Return (ats_type, slug) for the most-mentioned board, or None.

    'Most mentioned' handles pages that link to several things (e.g. a partner's board once,
    their own board on every job link).
    """
    counts: dict[tuple[AtsType, str], int] = {}
    text = url + "\n" + html
    for ats, pattern in ATS_PATTERNS:
        for match in pattern.finditer(text):
            slug = match.group(1).rstrip(".")
            if slug.lower() in NOT_A_SLUG:
                continue
            key = (ats, slug)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)
