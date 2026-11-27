"""Step B: who should the student write to? Every contact says exactly where it was found.

Sources, in the order we trust them:
  1. first-party: pages written by the company itself, already fetched in research.py:
     its own site, its YC company page, its Launch HN / Show HN post, its HN "Who is hiring?" post.
     Also a founder's own website (the one their GitHub profile names), fetched here.
  2. third-party: someone else's page about the company that research.py already read (another
     person's HN story, a news page), and the GitHub API, ONLY for GitHub profiles a first-party
     page linked to. (X bios: we never fetch X; GitHub's `twitter_username` gives us the handle.)
  3. api: an optional email-finder (Hunter.io, if HUNTER_API_KEY is set)

Rules enforced in code (not left to an LLM):
  - no contact without a source URL
  - emails: an address on the company's domain is always kept. An address on another domain (gmail,
    a university...) is kept ONLY if the company itself published it (first-party page): many small
    startups really do use gmail. Found only on a third-party source -> it's a personal address -> dropped.
  - phone numbers only from the company's own site (an official company line), never from elsewhere
  - no addresses, no personal phone numbers
  - a social link is tied to a person only if it sits on the same line as their name or its handle
    contains their name; otherwise it's skipped
  - a pattern guess ("{first}@acme.com") is stored as is_guess=1 with its evidence and is never
    the default recipient. We never build emails from patterns ourselves.
  - email check = DNS MX record of the domain only. No SMTP probing.
We never fetch LinkedIn, X, Apollo, RocketReach or ZoomInfo (config.BLOCKED_DOMAINS); a LinkedIn or
X profile URL that a company page links to is only stored as a link.
"""

import re
from typing import Protocol
from urllib.parse import unquote

from .. import config
from ..http import FetchError
from ..storage import db
from .models import FIRST_PARTY_KINDS, CompanyBrief, Contact, Page
from .research import fetch_page, on_domain

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
NOT_REAL = re.compile(r"\.(png|jpe?g|gif|svg|webp)$|@(example|domain|email|yourcompany|company)\.com$|"
                      r"@sentry|wixpress|noreply|no-reply|donotreply", re.I)
SOCIAL = {
    "linkedin": re.compile(r"^https?://([a-z]+\.)?linkedin\.com/in/([^/?#]+)", re.I),
    "x": re.compile(r"^https?://(www\.)?(x|twitter)\.com/(?!intent|share|home|search|i/)([A-Za-z0-9_]{2,15})/?$", re.I),
    "github": re.compile(r"^https?://(www\.)?github\.com/(?!features|about|orgs|sponsors|topics|login)([A-Za-z0-9-]+)/?$", re.I),
}
FOUNDER_LINE = re.compile(r"^Founder: ([^,]+), ([^.]+)\.", re.M)  # written by research.yc_pages

# Email local parts: who reads them?
DECISION_ROLE = re.compile(r"founder|\bceo\b|\bcto\b|head of eng|engineering (lead|manager)|vp eng|chief", re.I)
GOOD_ALIASES = {"founders", "jobs", "careers", "hiring", "talent", "recruiting", "internships", "interns", "people", "team"}
OK_ALIASES = {"hello", "hi", "contact", "info", "hey"}
BAD_ALIASES = {"sales", "support", "help", "privacy", "legal", "billing", "press", "media", "security", "abuse",
               "partners", "partnerships", "investors", "accounts", "marketing", "dmca", "gdpr"}
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------- people ----------


def people_on_pages(brief: CompanyBrief, pages: list[Page]) -> list[dict]:
    """Founders/team from the brief (already checked against their pages) + the YC founder lines."""
    people = {p.name.strip(): {"name": p.name.strip(), "role": p.role} for p in brief.people}
    for page in pages:
        if page.kind == "yc":
            for name, role in FOUNDER_LINE.findall(page.text):
                people.setdefault(name.strip(), {"name": name.strip(), "role": role.strip()})
    return list(people.values())


def name_parts(name: str) -> list[str]:
    return [p.lower() for p in re.findall(r"[A-Za-z]+", name) if len(p) >= 3]


def person_for_handle(handle: str, people: list[dict]) -> dict | None:
    """'carlossgg' or 'spencer.m' -> the person whose first or last name is inside it."""
    handle = handle.lower()
    for p in people:
        if any(part in handle for part in name_parts(p["name"])):
            return p
    return None


def person_near_link(page: Page, url: str, people: list[dict]) -> dict | None:
    """The person named closest above this link. Team pages put "Asha Rao", "CTO" and the icon
    links on separate lines, so we look at the link's line and up to 3 lines before it."""
    lines = page.text.splitlines()
    for i, line in enumerate(lines):
        if url in line:
            for nearby in reversed(lines[max(0, i - 3) : i + 1]):  # nearest line first
                for p in people:
                    if p["name"].lower() in nearby.lower():
                        return p
    return None


# ---------- first-party ----------


def emails_on_page(page: Page) -> list[str]:
    found = [unquote(l[7:]).split("?")[0] for l in page.links if l.lower().startswith("mailto:")]
    found += EMAIL.findall(page.text)
    out = []
    for e in found:
        e = e.strip().strip("\\/.,;:()<>[]\"'").lower()
        if EMAIL.fullmatch(e) and not NOT_REAL.search(e) and e not in out:
            out.append(e)
    return out


def own_domain(email: str, domain: str | None) -> bool:
    """Is this address on the company's domain (or a subdomain of it)?"""
    d = email.split("@")[1]
    return bool(domain) and (d == domain or d.endswith("." + domain))


def social_contacts(company_id: int, page: Page, people: list[dict], source_type: str, confidence: str) -> list[Contact]:
    """LinkedIn / X / GitHub links on a page that belong to a named person (a company's own social
    page is not a person to write to, so links we can't tie to someone are skipped)."""
    out = []
    for link in page.links:
        for kind, pattern in SOCIAL.items():
            m = pattern.match(link)
            if not m:
                continue
            person = person_near_link(page, link, people) or person_for_handle(m.groups()[-1], people)
            if person:
                out.append(Contact(
                    company_id=company_id, type=kind, value=link.rstrip("/"),
                    person_name=person["name"], person_role=person["role"], source_type=source_type,
                    source_url=page.url, confidence=confidence,
                ))
    return out


def first_party_contacts(company_id: int, domain: str | None, pages: list[Page], people: list[dict]) -> list[Contact]:
    out = []
    for page in pages:
        if page.kind not in FIRST_PARTY_KINDS:
            continue
        for email in emails_on_page(page):
            own = own_domain(email, domain)
            person = person_for_handle(email.split("@")[0], people)
            out.append(Contact(
                company_id=company_id, type="email", value=email,
                person_name=person and person["name"], person_role=person and person["role"],
                source_type="first_party", source_url=page.url,
                confidence="high" if own else "medium",
                evidence=None if own else f"not on the company domain ({domain}); published by the company on this page",
            ))
        if page.kind == "company" and on_domain(page.url, domain):  # official phone: their own site only
            for link in page.links:
                if link.lower().startswith("tel:"):
                    out.append(Contact(company_id=company_id, type="phone", value=unquote(link[4:]).strip(),
                                       source_type="first_party", source_url=page.url, confidence="high",
                                       evidence="company phone listed on its own website"))
        out += social_contacts(company_id, page, people, "first_party", "high" if page.kind == "yc" else "medium")
    return out


# ---------- third-party pages research.py already read ----------


def third_party_contacts(company_id: int, domain: str | None, pages: list[Page], people: list[dict]) -> list[Contact]:
    """Someone else's page about the company (another person's HN story, a news page). We keep only
    work emails on the company's domain (any other address there is personal) and social links tied
    to a named person. No phones: only the company's own site gives an official number."""
    out = []
    for page in pages:
        if page.kind in FIRST_PARTY_KINDS:
            continue
        for email in emails_on_page(page):
            if not own_domain(email, domain):
                continue
            person = person_for_handle(email.split("@")[0], people)
            out.append(Contact(
                company_id=company_id, type="email", value=email,
                person_name=person and person["name"], person_role=person and person["role"],
                source_type="third_party", source_url=page.url, confidence="medium",
                evidence="listed on a third-party page about the company",
            ))
        out += social_contacts(company_id, page, people, "third_party", "low")
    return out


# ---------- third-party: GitHub API ----------

GITHUB_USER_API = "https://api.github.com/users/{login}"
MAX_GITHUB_LOOKUPS = 5  # unauthenticated GitHub API allows 60 calls an hour


async def github_contacts(client, company_id: int, domain: str | None, found: list[Contact]) -> list[Contact]:
    """Public profile fields of GitHub users that a first-party page linked to a person."""
    out = []
    for c in [c for c in found if c.type == "github"][:MAX_GITHUB_LOOKUPS]:
        login = c.value.rstrip("/").rsplit("/", 1)[-1]
        try:
            user = await client.get_json(GITHUB_USER_API.format(login=login))
        except FetchError:
            continue
        common = dict(company_id=company_id, person_name=c.person_name, person_role=c.person_role,
                      source_type="third_party", source_url=f"https://github.com/{login}")
        email = (user.get("email") or "").lower()
        if email and own_domain(email, domain):
            out.append(Contact(type="email", value=email, confidence="medium",
                               evidence="public email on their GitHub profile", **common))
        # (an email on another domain here is a personal address: not collected)
        if user.get("blog"):
            blog = user["blog"] if user["blog"].startswith("http") else "https://" + user["blog"]
            out.append(Contact(type="website", value=blog, confidence="medium", **common))
        if user.get("twitter_username"):
            out.append(Contact(type="x", value=f"https://x.com/{user['twitter_username']}", confidence="medium", **common))
    return out


# ---------- first-party: a founder's own website ----------

MAX_FOUNDER_SITES = 3  # one page each (their homepage): enough to find a listed email or profile


async def founder_site_contacts(client, company_id: int, domain: str | None, found: list[Contact]) -> list[Contact]:
    """Read the personal website a founder's GitHub profile names (the `website` contacts above).
    It's their own page, so it counts as first-party, but with medium confidence: we only know it's
    theirs because GitHub says so. Kept: emails on the company domain (an address on another domain
    is personal) and social links whose handle contains their name. Blocked sites (LinkedIn, X...)
    are refused by PoliteClient, so they're never fetched."""
    out = []
    sites = [c for c in found if c.type == "website" and c.person_name and not on_domain(c.value, domain)]
    for site in sites[:MAX_FOUNDER_SITES]:
        page = await fetch_page(client, site.value, "founder_site", [])
        if page is None:
            continue
        person = [{"name": site.person_name, "role": site.person_role}]
        note = f"on {site.person_name}'s own website (named on their GitHub profile)"
        for email in emails_on_page(page):
            if own_domain(email, domain):
                who = person_for_handle(email.split("@")[0], person)  # hello@acme.dev is not the founder
                out.append(Contact(
                    company_id=company_id, type="email", value=email,
                    person_name=who and who["name"], person_role=who and who["role"],
                    source_type="first_party", source_url=page.url, confidence="medium", evidence=note,
                ))
        for link in page.links:
            for kind, pattern in SOCIAL.items():
                m = pattern.match(link)
                if m and person_for_handle(m.groups()[-1], person):  # their own profile, not a friend's
                    out.append(Contact(
                        company_id=company_id, type=kind, value=link.rstrip("/"), person_name=site.person_name,
                        person_role=site.person_role, source_type="first_party", source_url=page.url,
                        confidence="medium", evidence=note,
                    ))
    return out


# ---------- api: optional email finder ----------


class EmailFinder(Protocol):
    """An email-finder service, used ONLY through its official API."""

    async def find(self, client, company_id: int, domain: str, people: list[dict]) -> list[Contact]: ...


class HunterFinder:
    """Hunter.io domain search (free plan: no credit card, a few dozen searches a month).
    Docs: https://hunter.io/api-documentation/v2#domain-search"""

    URL = "https://api.hunter.io/v2/domain-search?domain={domain}&limit=10&api_key={key}"
    PUBLIC_URL = "https://hunter.io/search/{domain}"  # what we store: never the URL with the API key

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def find(self, client, company_id, domain, people):
        try:
            data = await client.get_json(self.URL.format(domain=domain, key=self.api_key))
        except FetchError:
            return []  # (not printing the error: its message would contain the API key)
        return parse_hunter(data.get("data") or {}, company_id, domain, people)


def parse_hunter(data: dict, company_id: int, domain: str, people: list[dict]) -> list[Contact]:
    out = []
    for e in data.get("emails") or []:
        value = (e.get("value") or "").lower()
        sources = [s.get("uri") for s in e.get("sources") or [] if s.get("uri")]
        if not value.endswith("@" + domain) or not sources:
            continue  # off-domain = personal; no source page = we can't show where it came from
        score = e.get("confidence") or 0
        name = " ".join(x for x in [e.get("first_name"), e.get("last_name")] if x) or None
        verified = (e.get("verification") or {}).get("status") == "valid"
        out.append(Contact(
            company_id=company_id, type="email", value=value, person_name=name, person_role=e.get("position"),
            source_type="api", source_url=sources[0], verified=verified,
            confidence="high" if score >= 90 else "medium" if score >= 70 else "low",
            evidence=f"Hunter API, confidence {score}, {len(sources)} source(s)" + (", verified" if verified else ""),
        ))
    pattern = data.get("pattern")
    if pattern:
        seen = {c.value for c in out}
        for p in people:
            parts = name_parts(p["name"])
            if not parts:
                continue
            guess = pattern.replace("{first}", parts[0]).replace("{last}", parts[-1]) \
                           .replace("{f}", parts[0][0]).replace("{l}", parts[-1][0]) + "@" + domain
            if "{" in guess or guess in seen:
                continue
            out.append(Contact(
                company_id=company_id, type="email", value=guess, person_name=p["name"], person_role=p["role"],
                source_type="api", source_url=HunterFinder.PUBLIC_URL.format(domain=domain), confidence="low",
                is_guess=True,
                evidence=f"UNVERIFIED GUESS: Hunter says the pattern is {pattern}@{domain} "
                         f"(seen in {len(data.get('emails') or [])} addresses)",
            ))
    return out


def default_finder() -> EmailFinder | None:
    return HunterFinder(config.HUNTER_API_KEY) if config.HUNTER_API_KEY else None


# ---------- MX check ----------


async def has_mx(domain: str) -> bool | None:
    """True = the domain has a mail server (MX record), False = it doesn't, None = couldn't check."""
    import dns.asyncresolver
    import dns.exception
    import dns.resolver

    try:
        await dns.asyncresolver.resolve(domain, "MX", lifetime=5)
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except dns.exception.DNSException:
        return None


# ---------- ranking ----------


def alias_rank(c: dict) -> int:
    """0 = a decision maker, 1 = a named person or a hiring inbox, 2 = a general inbox, 3 = the wrong inbox."""
    if c.get("person_role") and DECISION_ROLE.search(c["person_role"]):
        return 0
    if c["type"] == "email":
        local = c["value"].split("@")[0]
        if "founder" in local:  # founders@, acmefounders@...
            return 0
        if local in BAD_ALIASES:
            return 3
        if c.get("person_name") or local in GOOD_ALIASES:
            return 1
        return 2
    return 1 if c.get("person_name") else 2


def level(c: dict) -> float:
    """Your ranking: first-party work email > API-verified email > third-party listed email >
    social DM (LinkedIn/X) > apply URL. Inboxes we can't place (research@, data@...), sales/support
    inboxes and domains with no mail server drop below the social DMs (owner's choice)."""
    if c.get("is_guess"):
        return 9
    if c["type"] == "email":
        if alias_rank(c) == 3:
            return 4.5
        local = c["value"].split("@")[0]
        if not c.get("person_name") and alias_rank(c) == 2 and local not in OK_ALIASES:
            return 4.4  # an inbox we can't place (research@, data@...): a founder's DM is a better bet
        if c.get("mx_ok") == 0:
            return 4.6
        if c["source_type"] == "first_party":
            return 1
        return 2 if c.get("verified") else 3
    return {"linkedin": 4, "x": 4, "apply_url": 5}.get(c["type"], 6)


def off_domain(c: dict) -> int:
    return 1 if c["type"] == "email" and (c.get("evidence") or "").startswith("not on the company domain") else 0


def rank_contacts(contacts: list[dict]) -> list[dict]:
    return sorted(contacts, key=lambda c: (level(c), off_domain(c), alias_rank(c),
                                           CONFIDENCE_ORDER.get(c["confidence"], 3), c.get("id") or 0))


def default_recipient(contacts: list[dict]) -> dict | None:
    """The best contact the student can actually write to. Never a guess; GitHub/website/phone aren't recipients."""
    ranked = [c for c in rank_contacts(contacts) if level(c) < 6]
    return ranked[0] if ranked else None


# ---------- putting it together ----------


def dedupe(contacts: list[Contact]) -> list[Contact]:
    """Same type + value found twice: keep the more trustworthy one (first-party first, then higher confidence)."""
    order = {"first_party": 0, "api": 1, "third_party": 2}
    best: dict[tuple, Contact] = {}
    for c in contacts:
        key = (c.type, c.value.lower())
        old = best.get(key)
        if old is None or (order[c.source_type], CONFIDENCE_ORDER[c.confidence]) < (order[old.source_type], CONFIDENCE_ORDER[old.confidence]):
            if old and not c.person_name:
                c.person_name, c.person_role = old.person_name, old.person_role
            best[key] = c
        elif not old.person_name and c.person_name:
            old.person_name, old.person_role = c.person_name, c.person_role
    return list(best.values())


async def find_contacts(conn, client, company_id: int, pages: list[Page], brief: CompanyBrief,
                        apply_url: str | None = None, finder: EmailFinder | None = None, mx_check=has_mx) -> list[dict]:
    """Collect, check, save and return this company's contacts, best first."""
    company = db.get_company(conn, company_id)
    domain = company.domain
    people = people_on_pages(brief, pages)

    found = first_party_contacts(company_id, domain, pages, people)
    found += third_party_contacts(company_id, domain, pages, people)
    found += await github_contacts(client, company_id, domain, found)
    found += await founder_site_contacts(client, company_id, domain, found)
    if finder and domain:
        found += await finder.find(client, company_id, domain, people)
    url = apply_url or company.careers_url
    if url:
        found.append(Contact(company_id=company_id, type="apply_url", value=url, source_type="first_party",
                             source_url=url, confidence="high", evidence="apply / careers page"))
    found = [c for c in dedupe(found) if c.source_url]

    mx: dict[str, bool | None] = {}
    for c in found:
        if c.type == "email":
            d = c.value.split("@")[1]
            if d not in mx:
                mx[d] = await mx_check(d)
            c.mx_ok = mx[d]

    db.save_contacts(conn, company_id, [c.model_dump() for c in found])
    return rank_contacts(db.company_contacts(conn, company_id))


def describe(c: dict) -> str:
    """One line for the terminal: what, who, how sure, and where it came from."""
    who = f"{c['person_name']}" + (f" ({c['person_role']})" if c.get("person_role") else "") if c.get("person_name") else "company"
    tags = [c["source_type"].replace("_", "-"), f"{c['confidence']} confidence"]
    if c["type"] == "email":
        tags.append({1: "MX ok", 0: "NO MAIL SERVER", None: "MX not checked"}[c.get("mx_ok")])
    if c.get("verified"):
        tags.append("API-verified")
    label = "UNVERIFIED GUESS " if c.get("is_guess") else ""
    line = f"{label}{c['type']}: {c['value']}  -- {who}  [{', '.join(tags)}]\n      found at: {c['source_url']}"
    if c.get("evidence"):
        line += f"\n      note: {c['evidence']}"
    return line
