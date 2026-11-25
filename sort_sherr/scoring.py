"""quality_score: a simple, explainable 0-100 score for "is this a good startup to target?".

We have no free funding data, so we use YC signals as a proxy:
a recent batch means the company raised a seed round recently.
Every point below can be explained in one sentence.
"""

import re
from datetime import date

from .models import Company

# YC batch names look like "Winter 2024", "Summer 2023", "Fall 2024", "Spring 2025", or old style "W24"/"S23".
SEASON_MONTH = {"winter": 1, "w": 1, "spring": 4, "x": 4, "summer": 6, "s": 6, "fall": 9, "f": 9}


def batch_date(batch: str | None) -> date | None:
    if not batch:
        return None
    m = re.match(r"^\s*([A-Za-z]+)\s*'?(\d{2}|\d{4})\s*$", batch)
    if not m:
        return None
    season, year = m.group(1).lower(), int(m.group(2))
    if year < 100:
        year += 2000
    month = SEASON_MONTH.get(season)
    return date(year, month, 1) if month else None


def recency_points(batch: str | None, today: date | None = None) -> int:
    """40 points for a batch in the last 2 years, sliding to 0 at 6 years old."""
    d = batch_date(batch)
    if d is None:
        return 0
    years_old = ((today or date.today()) - d).days / 365
    if years_old <= 2:
        return 40
    if years_old >= 6:
        return 0
    return round(40 * (6 - years_old) / 4)


NON_YC_SCORE = 30  # e.g. only seen on HN: we keep it, but rank it below most YC companies


def score_breakdown(c: Company, today: date | None = None) -> list[tuple[str, int]]:
    """Every reason for the score, as (explanation, points). The web app shows this list."""
    if "yc" not in c.sources:
        return [("Not a YC company (e.g. only seen on HN): kept, but ranked lower", NON_YC_SCORE)]

    reasons = [(f"YC batch {c.yc_batch or '?'} (recent batch = recent funding)", recency_points(c.yc_batch, today))]
    if c.stage == "Growth":
        reasons.append(("Growth stage", 15))
    if c.top_company:
        reasons.append(("YC top company", 15))
    if c.is_hiring:
        reasons.append(("Marked as hiring", 15))
    if c.team_size and 5 <= c.team_size <= 200:
        reasons.append((f"Team of {c.team_size}: big enough to mentor, small enough to need interns", 15))
    if "hn" in c.sources:
        reasons.append(("Also posted in HN 'Who is hiring?'", 10))
    return reasons


def quality_score(c: Company, today: date | None = None) -> int:
    return min(sum(points for _, points in score_breakdown(c, today)), 100)
