"""Data shapes for Part 3.

- Page:          one public page (or headline) we read about a company, with the links on it
- CompanyBrief:  what the LLM learned from those pages; EVERY fact carries the URL it came from
- Contact:       one way to reach the company or a person, with where we found it
- DraftPair:     the LLM's two email variants for one company
- GroundingReport / JudgeScores: the checker's and the judge's answers

As in Part 2, models sent to Gemini as a response_schema use only simple types (no dicts).
"""

from pydantic import BaseModel, Field

# ---------- research ----------

# Where a page came from. "company", "yc", "hn_launch" and "hn_hiring" are written by the company
# itself (first-party); "news" and "hn" (someone else's HN story) are third-party.
FIRST_PARTY_KINDS = {"company", "yc", "hn_launch", "hn_hiring"}


class Page(BaseModel):
    url: str
    kind: str  # company / yc / hn_launch / hn_hiring / hn / news
    text: str  # visible text, links kept as [text](url)
    links: list[str] = Field(default_factory=list)  # every href on the page, incl. mailto: and tel:


class Fact(BaseModel):
    text: str = Field(description="one specific fact in plain words, only from the page")
    source_url: str = Field(description="the SOURCE url of the page that states it, copied exactly")
    date: str | None = Field(None, description="when it happened/was published, if the page says, e.g. 'Sep 2026'")


class Person(BaseModel):
    name: str
    role: str | None = Field(None, description="e.g. 'Co-founder & CTO', only if the page says")
    source_url: str = Field(description="the SOURCE url of the page that lists this person")


class CompanyBrief(BaseModel):
    what_they_build: list[Fact] = Field(description="1-2 facts: what the product is, in plain words")
    who_its_for: list[Fact] = Field(default_factory=list, description="customers / users")
    tech_stack: list[Fact] = Field(default_factory=list, description="languages, frameworks, infra they mention")
    recent_updates: list[Fact] = Field(default_factory=list, description="launches, releases, funding, news; with dates")
    hiring_areas: list[Fact] = Field(default_factory=list, description="open problems they describe or teams/roles they hire for")
    people: list[Person] = Field(default_factory=list, description="founders / team members named on the pages")


SECTIONS = ("what_they_build", "who_its_for", "tech_stack", "recent_updates", "hiring_areas")


def numbered_facts(brief: CompanyBrief) -> list[dict]:
    """Facts with stable ids F1, F2, ... (section order, then page order), so drafts can cite them."""
    out = []
    for section in SECTIONS:
        for f in getattr(brief, section):
            out.append({"id": f"F{len(out) + 1}", "section": section, **f.model_dump()})
    return out


# ---------- contacts ----------


class Contact(BaseModel):
    company_id: int
    type: str  # email / linkedin / x / github / website / phone / apply_url
    value: str
    person_name: str | None = None
    person_role: str | None = None
    source_type: str  # first_party / third_party / api
    source_url: str  # the exact page where we found it
    confidence: str  # high / medium / low
    mx_ok: bool | None = None  # emails only: does the domain accept mail? (DNS MX record)
    is_guess: bool = False  # a pattern guess (e.g. from Hunter): shown, labeled, never the default
    verified: bool = False  # an email-finder API says it verified this address
    evidence: str | None = None  # why we believe it / what the guess is based on


# ---------- drafts ----------


class EmailVariant(BaseModel):
    style: str = Field(description="'product-led' or 'project-led'")
    subject: str
    body: str = Field(description="plain text, 90-150 words, ends with the placeholders for links and [NAME]")
    facts_used: list[str] = Field(description="ids like F2 of every company fact the email mentions")
    student_items_used: list[str] = Field(description="ids like S1 of every student item the email mentions")


class DraftPair(BaseModel):
    variants: list[EmailVariant]


class Claim(BaseModel):
    variant: str = Field(description="A or B")
    claim: str = Field(description="one factual statement from the email, copied or closely paraphrased")
    about: str = Field(description="'company', 'student' or 'other' (greetings, the ask, availability)")
    support: str | None = Field(None, description="id of the fact (F..) or student item (S..) that backs it, or null")
    supported: bool


class GroundingReport(BaseModel):
    claims: list[Claim]


class JudgeScores(BaseModel):
    specificity: int = Field(description="1-5")
    grounding: int = Field(description="1-5")
    relevance: int = Field(description="1-5")
    length: int = Field(description="1-5")
    tone: int = Field(description="1-5")
    comment: str = Field(description="one sentence: the biggest weakness")

    def average(self) -> float:
        scores = [self.specificity, self.grounding, self.relevance, self.length, self.tone]
        return round(sum(max(1, min(5, s)) for s in scores) / 5, 2)
