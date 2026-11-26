"""Data shapes for Part 2.

- ParsedResume:     what the LLM extracts from the (redacted) resume text
- CandidateProfile: ParsedResume + the personal bits we kept LOCAL (name, links)
- Preferences:      what the student tells us on the command line (not from the resume)
- RerankResult:     the LLM's verdict on one company for this student

Note: models sent to Gemini as a response_schema use only simple types (str, int, list, nested
models). Gemini's schema support doesn't handle dicts well, so no dict fields there.
"""

import re

from pydantic import BaseModel, Field

# Roles (or degrees) for graduate students: "Research Intern (PhD)", "MS student", "Doctoral".
# An undergrad can't take these, so they don't count as a fitting job or an intern posting.
GRAD_ONLY = re.compile(r"\bph\.?\s?d\b|\bdoctora|\bmasters?\b|\bm\.?s\.? students?\b|\bm\.?tech\b", re.I)

# ---------- resume ----------


class Education(BaseModel):
    college: str | None = None
    degree: str | None = None  # "B.Tech", "BS", "Integrated M.Tech"
    branch: str | None = None  # "Computer Science", "Electrical"
    year: int | None = Field(None, description="current year of study (1-5), null if not stated")
    grad_date: str | None = Field(None, description="expected graduation, e.g. '2027' or 'May 2027'")


class Project(BaseModel):
    title: str
    description: str | None = Field(None, description="1-2 sentences, from the resume only")
    tech_stack: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list, description="copy placeholders like [LINK_2] exactly")
    impact: str | None = Field(None, description="numbers/results if stated, e.g. '10k users', '30% faster'")


class Experience(BaseModel):
    role: str
    organization: str | None = None
    start: str | None = None
    end: str | None = None
    description: str | None = None
    tech_stack: list[str] = Field(default_factory=list)


class ParsedResume(BaseModel):
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list, description="normalized names, e.g. 'React' not 'ReactJS'")
    projects: list[Project] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    focus_areas: list[str] = Field(
        default_factory=list, description="2-4 areas inferred from projects/experience, e.g. backend, ML, GenAI"
    )


class CandidateProfile(ParsedResume):
    """Everything above, plus personal fields that were redacted before the LLM call
    and filled back in locally."""

    name: str | None = None
    links: list[str] = Field(default_factory=list)  # GitHub / LinkedIn / portfolio URLs


# ---------- preferences (from the CLI, never from the resume) ----------

ROLE_CHOICES = ["any", "sde", "backend", "frontend", "fullstack", "ai-ml", "data", "mobile", "devops", "design", "product"]


class Preferences(BaseModel):
    roles: list[str] = Field(default_factory=lambda: ["any"])
    remote_only: bool = False
    locations: list[str] = Field(default_factory=list)  # e.g. ["India", "Bangalore"]; empty = anywhere
    paid_only: bool = False
    window: str | None = None  # e.g. "Summer 2027"
    interests: str | None = None  # the student's own words: what they like, what they're looking for
    shortlist: int = 15  # how many companies the student wants in the final list


# ---------- rerank ----------

SUB_SCORES = ("stack_overlap", "domain_fit", "interest_fit", "seniority_fit", "hiring_signal")  # 0-20 each


class Evidence(BaseModel):
    resume_item: str = Field(description="a specific project/skill/experience from the resume, or the student's stated interest")
    company_fact: str = Field(description="a specific fact copied from the company info (product, tag, job title, requirement)")


class RerankResult(BaseModel):
    company_id: int
    stack_overlap: int = Field(description="0-20")
    domain_fit: int = Field(description="0-20")
    interest_fit: int = Field(description="0-20; 10 if the student gave no interests")
    seniority_fit: int = Field(description="0-20: would they plausibly take an intern?")
    hiring_signal: int = Field(description="0-20: intern posting > engineering posting > 'is hiring' > nothing")
    why_fit: str = Field(description="1-2 sentences to the student: why THIS company suits THEM")
    evidence: list[Evidence] = Field(description="2-3 items")
    best_job_id: int | None = Field(None, description="id of the best listed job, or null")
    why_this_role: str | None = Field(None, description="1 sentence on why that job suits them, if best_job_id is set")
    pitch_role: str | None = Field(
        None, description="the internship the student should ask this company for, e.g. 'Backend engineering intern'"
    )
    red_flags: list[str] = Field(default_factory=list)

    def sub_scores(self) -> dict[str, int]:
        return {k: max(0, min(20, getattr(self, k))) for k in SUB_SCORES}

    def total(self) -> int:
        return sum(self.sub_scores().values())  # 5 x 20 = 0-100


class RerankBatch(BaseModel):
    results: list[RerankResult]
