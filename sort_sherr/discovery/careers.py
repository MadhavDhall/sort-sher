"""Find a company's careers page, then detect its ATS.

Steps:
  1. Fetch the homepage. If it links straight to an ATS board, we're done.
  2. Otherwise find a careers link (by URL like /careers or by text like "Join us").
  3. If there's no such link, try the usual paths /careers and /jobs.
  4. Fetch the careers page and look for an ATS there.
  5. If nothing was found and it's a YC company, use its public YC jobs page as a last resort.
"""

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..http import FetchError, PoliteClient, is_blocked_domain
from ..models import AtsType, Company
from .ats import detect_ats

LINK_TEXT = re.compile(
    r"\b(careers?|jobs|hiring|join (us|the team|our team)|open (roles|positions)|work (with|at) us)\b", re.I
)
LINK_HREF = re.compile(r"/(careers?|jobs|join(-us)?|hiring|work-with-us|open-roles)(/|$|\?|#|\.html?)", re.I)
COMMON_PATHS = ("/careers", "/jobs")


@dataclass
class Discovery:
    status: str  # "ats", "careers_page", "yc_jobs_page", or an error label
    careers_url: str | None = None
    ats_type: AtsType | None = None
    ats_slug: str | None = None


def find_careers_link(html: str, base_url: str) -> str | None:
    """Best careers-looking link on the page, as an absolute URL."""
    candidates = []
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href)
        if is_blocked_domain(urlparse(url).hostname or ""):
            continue  # e.g. linkedin.com/company/x/jobs
        if detect_ats("", url):
            return url  # a direct link to an ATS board beats everything
        text = a.text(strip=True)
        if LINK_HREF.search(urlparse(url).path + "/") or LINK_TEXT.search(text):
            candidates.append(url)
    return candidates[0] if candidates else None


def yc_jobs_url(company: Company) -> str | None:
    return f"https://www.ycombinator.com/companies/{company.yc_slug}/jobs" if company.yc_slug else None


async def discover(client: PoliteClient, company: Company) -> Discovery:
    fallback = yc_jobs_url(company)

    # Already know a careers page (e.g. linked in an HN post)? Trust it and just check it for an ATS.
    if company.careers_url:
        try:
            page = await client.get(company.careers_url)
            page_url = str(page.url)
            if found := detect_ats(page.text, page_url):
                return Discovery("ats", page_url, *found)
            return Discovery("careers_page", page_url)
        except FetchError:
            pass  # link is dead -> fall through to the normal search
    if not company.website:
        return Discovery("yc_jobs_page", fallback) if fallback else Discovery("no_website")

    try:
        home = await client.get(company.website)
    except FetchError as e:
        return Discovery("yc_jobs_page", fallback) if fallback else Discovery(f"homepage_{e.label}")
    home_url = str(home.url)

    if found := detect_ats(home.text, home_url):
        return Discovery("ats", home_url, *found)

    careers_url = find_careers_link(home.text, home_url)
    page = None
    if careers_url:
        try:
            page = await client.get(careers_url)
        except FetchError:
            page = None
    if page is None:
        for path in COMMON_PATHS:
            try:
                page = await client.get(urljoin(home_url, path))
                break
            except FetchError:
                continue

    if page is not None:
        page_url = str(page.url)
        if found := detect_ats(page.text, page_url):
            return Discovery("ats", page_url, *found)
        return Discovery("careers_page", page_url)

    return Discovery("yc_jobs_page", fallback) if fallback else Discovery("no_careers_page")
