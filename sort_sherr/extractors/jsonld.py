"""Tier 2: schema.org JobPosting JSON-LD (https://schema.org/JobPosting).

Many sites embed a machine-readable block like this so Google Jobs can index them:
  <script type="application/ld+json">{"@type": "JobPosting", "title": "...", ...}</script>

Listing pages often don't have it, but each job's own page usually does. So we:
  1. look on the careers page itself, and if nothing is there,
  2. follow links that sit "under" the careers page (e.g. /careers -> /careers/backend-engineer).
"""

import json
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..http import FetchError, PoliteClient
from ..models import JobDraft, guess_is_internship
from .text import html_to_text, parse_date

MAX_DETAIL_PAGES = 15  # never follow more than this many job links per company


def find_job_postings(html: str) -> list[dict]:
    """All JobPosting objects in the page's JSON-LD blocks (handles lists and @graph)."""
    found = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" in types:
                found.append(node)
            walk(node.get("@graph"))

    for script in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.text(), strict=False))
        except json.JSONDecodeError:
            continue  # broken JSON on someone's site -- skip that block
    return found


def _location(posting: dict) -> str | None:
    places = posting.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    names = []
    for place in places:
        addr = place.get("address") if isinstance(place, dict) else None
        if isinstance(addr, str):
            names.append(addr)
        elif isinstance(addr, dict):
            parts = [addr.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
            parts = [p if isinstance(p, str) else (p or {}).get("name") for p in parts]
            names.append(", ".join(p for p in parts if p))
    return "; ".join(n for n in names if n) or None


def to_job(posting: dict, page_url: str) -> JobDraft | None:
    title = (posting.get("title") or "").strip()
    if not title:
        return None
    employment_type = posting.get("employmentType")
    if isinstance(employment_type, list):
        employment_type = ", ".join(employment_type)
    requirements = [posting.get(k) for k in ("qualifications", "experienceRequirements", "skills")]
    requirements = [r if isinstance(r, str) else json.dumps(r) for r in requirements if r]
    location = _location(posting)
    return JobDraft(
        title=title,
        location=location,
        # schema.org says remote jobs use jobLocationType "TELECOMMUTE"
        remote=True if posting.get("jobLocationType") == "TELECOMMUTE" else None,
        is_internship=guess_is_internship(title, employment_type),
        employment_type=employment_type,
        description=html_to_text(posting.get("description")),
        requirements=html_to_text("\n".join(requirements)) if requirements else None,
        apply_url=posting.get("url") or page_url,
        posted_at=parse_date(posting.get("datePosted")),
    )


def extract_from_html(html: str, page_url: str) -> list[JobDraft]:
    jobs = [to_job(p, page_url) for p in find_job_postings(html)]
    return [j for j in jobs if j]


def child_links(html: str, page_url: str) -> list[str]:
    """Links on the same host whose path sits under this page's path.
    e.g. on /companies/mem0/jobs -> /companies/mem0/jobs/85yRXxn-forward-deployed-engineer"""
    base = urlparse(page_url)
    prefix = base.path.rstrip("/") + "/"
    if prefix == "/":
        return []  # careers "page" is the homepage: every link would be a child, so don't guess
    links = []
    for a in HTMLParser(html).css("a[href]"):
        url = urljoin(page_url, a.attributes.get("href") or "").split("#")[0]
        p = urlparse(url)
        if p.hostname == base.hostname and p.path.startswith(prefix) and len(p.path) > len(prefix):
            if url not in links:
                links.append(url)
    return links[:MAX_DETAIL_PAGES]


async def extract_jobs(client: PoliteClient, html: str, page_url: str) -> list[JobDraft]:
    """Tier 2 entry point: the page itself, then its child job pages."""
    jobs = extract_from_html(html, page_url)
    if jobs:
        return jobs
    for link in child_links(html, page_url):
        try:
            detail = await client.get(link)
        except FetchError:
            continue
        jobs.extend(extract_from_html(detail.text, link))
    return jobs
