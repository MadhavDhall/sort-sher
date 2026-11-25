"""The data shapes used everywhere.

- Company:  one startup (from YC, HN, ...)
- JobDraft: what every extractor (tier 1, 2, 3) returns -- ONE normalized schema
- Job:      a JobDraft after it has been saved (has ids + seen timestamps)
"""

import re
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator


class AtsType(str, Enum):
    greenhouse = "greenhouse"
    lever = "lever"
    ashby = "ashby"
    workable = "workable"
    rippling = "rippling"
    dover = "dover"
    gem = "gem"


class Company(BaseModel):
    id: int | None = None
    name: str
    website: str | None = None
    domain: str | None = None  # "stripe.com" -- used to match the same company across sources
    sources: list[str] = Field(default_factory=list)  # e.g. ["yc", "hn"]
    yc_slug: str | None = None  # e.g. "mem0" -> ycombinator.com/companies/mem0
    yc_batch: str | None = None
    team_size: int | None = None
    is_hiring: bool | None = None
    stage: str | None = None  # YC "Early" / "Growth"
    top_company: bool | None = None
    locations: list[str] = Field(default_factory=list)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    contact: str | None = None  # e.g. email from an HN post
    quality_score: int = 0
    careers_url: str | None = None
    ats_type: AtsType | None = None
    ats_slug: str | None = None
    discover_status: str | None = None  # "ats" / "careers_page" / an error label
    crawl_status: str | None = None  # "ok" / "no_jobs" / an error label
    crawl_error: str | None = None
    last_crawled_at: datetime | None = None


class JobDraft(BaseModel):
    """A job as found by an extractor, before it is stored."""

    title: str
    team: str | None = None
    location: str | None = None
    remote: bool | None = None  # None = unknown
    is_internship: bool | None = None  # None = unknown
    employment_type: str | None = None
    description: str | None = None
    requirements: str | None = None
    apply_url: str | None = None
    posted_at: datetime | None = None

    @model_validator(mode="after")
    def clean_up(self):
        """Runs automatically every time a JobDraft is made, whichever tier made it.
        One place for the rules, so every source gets cleaned the same way."""
        self.title = " ".join(self.title.split())  # collapse stray spaces / newlines
        raw_type = self.employment_type
        self.employment_type = normalize_employment_type(raw_type)
        if self.remote is None:  # the source didn't say -> look at the words
            self.remote = guess_remote(self.location, self.title)
        if self.is_internship is not True and guess_is_internship(self.title, raw_type):
            self.is_internship = True  # an intern word in the title/type always wins
        if self.posted_at and self.posted_at.tzinfo:
            # one time zone for everything, so sorting by date is correct
            self.posted_at = self.posted_at.astimezone(timezone.utc)
        return self


class Job(JobDraft):
    id: int | None = None
    company_id: int
    source_tier: int  # 1 = ATS API, 2 = JSON-LD, 3 = LLM
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool = True


def domain_of(url: str | None) -> str | None:
    """'https://www.Stripe.com/jobs' -> 'stripe.com'"""
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.") or None


# Word boundaries (\b) so "Internal Tools Engineer" or "International Sales" don't match.
# Also common non-English / non-US words for student roles (German Werkstudent, French stagiaire...).
INTERN_PATTERN = re.compile(
    r"\b(interns?|internships?|co-?op|summer analyst|summer associate|working student|werkstudent(in)?|"
    r"trainee(ship)?|apprentice(ship)?|stagiaire|praktikum|praktikant(in)?)\b",
    re.I,
)


def guess_is_internship(title: str, employment_type: str | None = None) -> bool:
    """Simple keyword check. Returns True/False (title is always known)."""
    return bool(INTERN_PATTERN.search(f"{title} {employment_type or ''}"))


NOT_REMOTE = re.compile(r"\b(no|not|non)[- ]remote\b", re.I)
REMOTE = re.compile(r"\bremote\b", re.I)


def guess_remote(*texts: str | None) -> bool | None:
    """True if any text says remote, False if it says "not remote", else None (unknown -- not False!)."""
    joined = " ".join(t for t in texts if t)
    if NOT_REMOTE.search(joined):
        return False
    return True if REMOTE.search(joined) else None


# Every ATS spells job types differently ("FullTime", "FULL_TIME", "Exempt: Full-Time", ...).
# We map them to a few clean labels so the badges and filters are consistent. Checked in order.
EMPLOYMENT_TYPES = [
    ("Internship", INTERN_PATTERN),
    ("Full-time", re.compile(r"full[\s_-]?time|\bpermanent\b|\bcdi\b", re.I)),
    ("Part-time", re.compile(r"part[\s_-]?time|fractional", re.I)),
    ("Contract", re.compile(r"\bcontract(or)?\b|freelance|hourly", re.I)),
    ("Temporary", re.compile(r"\btemp(orary)?\b|seasonal", re.I)),
]


def normalize_employment_type(raw: str | None) -> str | None:
    """'FULL_TIME' / 'Exempt: Full-Time' / 'full_time' -> 'Full-time'. Unknown text -> None."""
    if not raw:
        return None
    for label, pattern in EMPLOYMENT_TYPES:
        if pattern.search(raw):
            return label
    return None
