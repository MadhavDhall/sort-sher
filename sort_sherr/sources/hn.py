"""Seed companies from the latest HN "Ask HN: Who is hiring?" thread (via the HN Algolia API).

Posts usually start with a header line like:
    Acme (YC W24) | Backend Engineer, Intern | Bangalore, India | REMOTE | Full-time | https://acme.com
There is no strict format, so parsing is a best-effort heuristic: we split the header on "|"
and label each part (role, location, remote, url, ...) with simple keyword rules.
"""

import html as html_lib
import re
from urllib.parse import urlparse

from pydantic import BaseModel

from ..discovery.ats import detect_ats
from ..http import PoliteClient, is_blocked_domain
from ..models import AtsType, Company, JobDraft, domain_of, guess_is_internship, INTERN_PATTERN
from ..extractors.text import html_to_text, parse_date

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"

ROLE_WORDS = re.compile(
    r"\b(engineer|developer|scientist|designer|manager|intern|lead|architect|analyst|researcher|"
    r"founding|head of|director|sre|devops|marketing|sales|recruiter|product|ml|ai|frontend|backend|"
    r"full[- ]?stack|consultant|operations|associate|specialist|writer)\b",
    re.I,
)
REMOTE_WORDS = re.compile(r"\bremote\b", re.I)
ONSITE_WORDS = re.compile(r"\b(on-?site|in[- ]office|hybrid)\b", re.I)
EMPLOYMENT_WORDS = re.compile(r"\b(full[- ]?time|part[- ]?time|contract|freelance|permanent|hourly)\b", re.I)
MONEY = re.compile(r"[$€£₹]|\b\d+\s?k\b|\bequity\b|\bsalary\b|\bvisa\b", re.I)
URL_LIKE = re.compile(r"https?://|www\.|\.(com|io|ai|co|dev|app|org|net|xyz)\b", re.I)
HREF = re.compile(r'href="([^"]+)"')
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class HNPost(BaseModel):
    hn_id: int
    company: str
    header: str
    roles: list[str] = []
    location: str | None = None
    remote: bool | None = None
    employment_type: str | None = None
    website: str | None = None
    contact: str | None = None
    ats_type: AtsType | None = None
    ats_slug: str | None = None
    mentions_internship: bool = False
    text: str
    posted_at: str | None = None

    @property
    def url(self) -> str:
        return f"https://news.ycombinator.com/item?id={self.hn_id}"


def find_email(text: str) -> str | None:
    # undo common obfuscation: "jobs [at] acme [dot] com", "jobs (at) acme.com"
    t = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text, flags=re.I)
    t = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", t, flags=re.I)
    m = EMAIL.search(t)
    return m.group(0).rstrip(".") if m else None


def pick_website(company: str, header_parts: list[str], links: list[str]) -> str | None:
    """A URL in the header wins. Otherwise, the first link whose domain contains the company's name."""
    for part in header_parts:
        if URL_LIKE.search(part) and " " not in part.strip():
            url = part.strip()
            return url if url.startswith("http") else "https://" + url
    name_token = re.sub(r"[^a-z0-9]", "", company.lower())[:12]
    for link in links:
        d = domain_of(link) or ""
        if name_token and name_token in d.replace("-", "") and not is_blocked_domain(d) and not detect_ats("", link):
            return link
    return None


def parse_post(comment: dict) -> HNPost | None:
    raw_html = comment.get("text") or ""
    paragraphs = re.split(r"<p>", raw_html)
    header_html = next((p for p in paragraphs if "|" in p), None)
    if header_html is None:
        return None  # no "Company | ..." line -- not a standard job post
    header = html_to_text(header_html) or ""
    parts = [p.strip() for p in header.split("|") if p.strip()]
    if not parts:
        return None

    company = re.sub(r"\s*\(.*?\)\s*", " ", parts[0]).strip()  # "Shepherd (Series B)" -> "Shepherd"
    if not company or len(company) > 60:
        return None

    roles, location, employment = [], None, None
    for part in parts[1:]:
        if URL_LIKE.search(part) or MONEY.search(part):
            continue
        if REMOTE_WORDS.search(part) or ONSITE_WORDS.search(part):
            # "REMOTE (US)" carries no city; but "Austin, TX (hybrid)" does
            if "," in part and location is None:
                location = part
            continue
        if EMPLOYMENT_WORDS.search(part):
            employment = employment or part
            continue
        if ROLE_WORDS.search(part):
            roles.append(part)
        elif location is None:
            location = part

    remote = True if REMOTE_WORDS.search(header) else (False if ONSITE_WORDS.search(header) else None)
    links = [html_lib.unescape(h) for h in HREF.findall(raw_html)]
    found_ats = detect_ats(" ".join(links))
    text = html_to_text(raw_html) or ""

    return HNPost(
        hn_id=comment["id"],
        company=company,
        header=header,
        roles=roles,
        location=location,
        remote=remote,
        employment_type=employment,
        website=pick_website(company, parts, links),
        contact=find_email(text),
        ats_type=found_ats[0] if found_ats else None,
        ats_slug=found_ats[1] if found_ats else None,
        mentions_internship=bool(INTERN_PATTERN.search(text)),
        text=text,
        posted_at=comment.get("created_at"),
    )


def to_company(post: HNPost) -> Company:
    # The post often links a specific page (acme.com/jobs/devops): keep that as the careers URL
    # and the bare host as the website.
    homepage = careers_url = None
    if post.website:
        parsed = urlparse(post.website)
        homepage = f"{parsed.scheme}://{parsed.netloc}"
        if parsed.path.strip("/"):
            careers_url = post.website
    return Company(
        name=post.company,
        website=homepage,
        domain=domain_of(post.website),
        careers_url=careers_url,
        sources=["hn"],
        is_hiring=True,
        locations=[post.location] if post.location else [],
        description=post.header,
        tags=["hn-mentions-internships"] if post.mentions_internship else [],
        contact=post.contact,
        ats_type=post.ats_type,
        ats_slug=post.ats_slug,
        # The post already links an ATS board -> nothing left to discover.
        discover_status="ats" if post.ats_type else None,
    )


def to_job(post: HNPost) -> JobDraft:
    """The HN post itself, stored as a job with source_tier 0 (human-written post, not crawled)."""
    title = ", ".join(post.roles) or "(see HN post)"
    return JobDraft(
        title=title[:200],
        location=post.location,
        remote=post.remote,
        # Intern word in the HEADER line -> True ("... | Full-time | INTERNS | Zurich").
        # A mention deep in the text ("our interns love...") is too loose for the job filter;
        # it only tags the company (see to_company). Roles listed, none intern -> False; else unknown.
        is_internship=True
        if guess_is_internship(post.header, post.employment_type)
        else (False if post.roles else None),
        employment_type=post.employment_type,
        description=post.text,
        apply_url=post.url,
        posted_at=parse_date(post.posted_at),
    )


async def fetch_latest_thread(client: PoliteClient) -> dict:
    hits = (await client.get_json(SEARCH_URL))["hits"]
    story = next(h for h in hits if h["title"].lower().startswith("ask hn: who is hiring"))
    return await client.get_json(ITEM_URL.format(id=story["objectID"]))


def parse_thread(thread: dict) -> list[HNPost]:
    posts = [parse_post(c) for c in thread.get("children", []) if c.get("text")]
    return [p for p in posts if p]
