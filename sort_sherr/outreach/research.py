"""Step A: research one company from public pages, on demand (only for top matches).

Flow:
  cached brief younger than BRIEF_MAX_AGE_DAYS? -> use it
  else gather pages:
    - the company's homepage + up to 5 of its own pages that look useful (about, team, blog, changelog...)
    - its careers page (known from Part 1)
    - its YC company page: description, founders, jobs, Launch YC posts, news headlines
    - Hacker News stories about it (Launch HN / Show HN / news links), via the HN Algolia API
    - its own post in HN "Who is hiring?" (already in our database from Part 1)
  -> one LLM call: pages -> CompanyBrief (every fact names its source page)
  -> clean_brief(): drop any fact whose source isn't a page we actually read, and any person
     whose name isn't on their source page
  -> save brief + pages (contacts.py reads contacts from the same pages, no second fetch)

All fetching goes through PoliteClient (rate limits, honest User-Agent, blocked domains, robots.txt
when switched on). Only public pages are sent to the LLM.
"""

import html as html_lib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .. import outreach_config as cfg
from ..extractors import browser
from ..extractors.llm import MIN_TEXT_CHARS, html_to_markdown
from ..extractors.text import html_to_text
from ..http import FetchError
from ..models import domain_of
from ..storage import db
from . import fill, load_prompt
from .models import CompanyBrief, Page

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage=30"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"
YC_PAGE_URL = "https://www.ycombinator.com/companies/{slug}"

# Pages on the company's site worth reading, best first. Matched against the link's path and text.
USEFUL_PAGES = ["about", "team", "company", "blog", "changelog", "news", "product", "docs", "careers",
                "jobs", "contact", "research", "customers"]
SKIP_PAGES = re.compile(r"privacy|terms|legal|cookie|login|log-in|signin|sign-in|signup|sign-up|status|"
                        r"\.(pdf|png|jpe?g|svg|gif|zip|mp4)$", re.I)


def norm_url(url: str) -> str:
    """For comparing URLs: no #fragment, no trailing slash, lowercase host."""
    p = urlparse(url.strip())
    return f"{p.scheme.lower()}://{(p.netloc or '').lower()}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def on_domain(url: str, domain: str | None) -> bool:
    host = domain_of(url) or ""
    return bool(domain) and (host == domain or host.endswith("." + domain))


# ---------- page builders ----------


def page_from_html(url: str, kind: str, html: str) -> Page:
    """Visible text (links kept as [text](url)) + every href on the page, including mailto: and tel:."""
    links = []
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript:")):
            full = href if href.startswith(("mailto:", "tel:")) else urljoin(url, href)
            if full not in links:
                links.append(full)
    return Page(url=url, kind=kind, text=html_to_markdown(html, url)[: cfg.PAGE_CHARS], links=links)


def pick_site_links(home: Page, domain: str | None, limit: int) -> list[str]:
    """The most useful-looking pages on the company's own site, best first."""
    scored = {}
    for link in home.links:
        if not link.startswith("http") or not on_domain(link, domain) or SKIP_PAGES.search(link):
            continue
        path = urlparse(link).path.lower()
        if norm_url(link) == norm_url(home.url) or path in ("", "/"):
            continue
        for rank, word in enumerate(USEFUL_PAGES):
            if word in path:
                key = norm_url(link)
                scored[key] = min(scored.get(key, 99), rank)
                break
    return sorted(scored, key=lambda u: (scored[u], len(u)))[:limit]


def yc_pages(html: str, url: str) -> list[Page]:
    """The YC company page keeps its data as JSON in a `data-page` attribute. We turn it into plain
    text pages: the company page itself (description, founders + their public links, open roles),
    one page per Launch YC post, and one per news headline listed there."""
    m = re.search(r'data-page="([^"]+)"', html)
    if not m:
        return [page_from_html(url, "yc", html)]
    props = json.loads(html_lib.unescape(m.group(1))).get("props", {})
    c = props.get("company") or {}
    lines = [f"{c.get('name')}: {c.get('one_liner') or ''}", c.get("long_description") or ""]
    for label, key in [("YC batch", "batch_name"), ("Founded", "year_founded"), ("Team size", "team_size"),
                       ("Location", "location")]:
        if c.get(key):
            lines.append(f"{label}: {c[key]}")
    links = [c[k] for k in ("linkedin_url", "twitter_url", "github_url") if c.get(k)]
    for f in c.get("founders") or []:
        socials = [(label, f.get(key)) for label, key in [("LinkedIn", "linkedin_url"), ("X", "twitter_url")] if f.get(key)]
        links += [u for _, u in socials]
        social_text = " ".join(f"[{label}]({u})" for label, u in socials)
        lines.append(f"Founder: {f.get('full_name')}, {f.get('title') or 'founder'}. {f.get('founder_bio') or ''} {social_text}".strip())
    for j in props.get("jobPostings") or []:
        details = ", ".join(x for x in [j.get("type"), j.get("location"), j.get("salaryRange")] if x)
        lines.append(f"Open role: {j.get('title')} ({details})")
    pages = [Page(url=url, kind="yc", text="\n".join(l for l in lines if l)[: cfg.PAGE_CHARS], links=links)]

    for launch in (props.get("launches") or [])[:3]:
        if launch.get("url"):
            text = f"Launch YC post \"{launch.get('title')}\" (posted {(launch.get('created_at') or '')[:10]}). " \
                   f"{launch.get('tagline') or ''}\n{launch.get('body') or ''}"
            pages.append(Page(url=launch["url"], kind="yc", text=text[: cfg.PAGE_CHARS]))
    for n in (props.get("newsItems") or [])[: cfg.MAX_NEWS_ITEMS]:
        if n.get("url") and n.get("title"):
            text = f"News headline: \"{n['title']}\" (published {n.get('date') or 'date unknown'}). Listed on the company's YC page."
            pages.append(Page(url=n["url"], kind="news", text=text))
    return pages


def relevant_story(hit: dict, name: str, domain: str | None) -> str | None:
    """Is this HN story about the company? Returns the page kind ("hn_launch" = posted by the company,
    "hn" = someone else's story) or None. Strict, because many startups share a name with something."""
    title = hit.get("title") or ""
    if re.match(rf"(launch|show) hn: {re.escape(name)}\b", title, re.I):
        return "hn_launch"
    if hit.get("url") and on_domain(hit["url"], domain):
        return "hn_launch" if title.lower().startswith(("launch hn", "show hn")) else "hn"
    if re.search(rf"\b{re.escape(name)}\b", title) and (re.search(r"\bYC\b|Y Combinator", title) or len(name) >= 8):
        return "hn"
    return None


def hn_pages(search: dict, name: str, domain: str | None) -> list[Page]:
    pages = []
    hits = sorted(search.get("hits") or [], key=lambda h: h.get("created_at") or "", reverse=True)
    for hit in hits:
        kind = relevant_story(hit, name, domain)
        if not kind:
            continue
        text = f"Hacker News story \"{hit.get('title')}\" (posted {(hit.get('created_at') or '')[:10]}, " \
               f"{hit.get('points') or 0} points)."
        if hit.get("url"):
            text += f" It links to {hit['url']}"
        text += "\n" + (html_to_text(hit.get("story_text")) or "")
        pages.append(Page(url=HN_ITEM_URL.format(id=hit["objectID"]), kind=kind, text=text[: cfg.PAGE_CHARS]))
        if len(pages) >= cfg.MAX_HN_STORIES:
            break
    return pages


def hn_hiring_pages(conn, company_id: int) -> list[Page]:
    """The company's own post(s) in HN "Who is hiring?" (stored by Part 1 as tier-0 jobs)."""
    pages = []
    for job in db.company_jobs(conn, company_id):
        if job["source_tier"] == 0 and job["apply_url"] and job["description"]:
            text = f"The company's post in HN 'Who is hiring?' ({(job['posted_at'] or '')[:10]}):\n{job['description']}"
            pages.append(Page(url=job["apply_url"], kind="hn_hiring", text=text[: cfg.PAGE_CHARS]))
    return pages[:1]  # the newest month is enough


# ---------- gathering ----------


async def fetch_page(client, url: str, kind: str, notes: list[str]) -> Page | None:
    try:
        resp = await client.get(url)
    except FetchError as e:
        notes.append(f"{url}: {e.label}")
        return None
    return page_from_html(str(resp.url), kind, resp.text)


async def gather_pages(client, conn, company) -> tuple[list[Page], list[str]]:
    """company: a models.Company. Returns (pages, notes about pages we couldn't read)."""
    notes: list[str] = []
    pages: list[Page] = []
    domain = company.domain

    if company.website:
        home = await fetch_page(client, company.website, "company", notes)
        if home and len(home.text) < MIN_TEXT_CHARS and browser.available():
            rendered = await browser.render(company.website)  # JavaScript-only homepage
            if rendered:
                home = page_from_html(home.url, "company", rendered)
        if home:
            pages.append(home)
            for link in pick_site_links(home, domain, cfg.MAX_SITE_PAGES - 1):
                if page := await fetch_page(client, link, "company", notes):
                    pages.append(page)

    seen = {norm_url(p.url) for p in pages}
    careers = company.careers_url
    if careers and "ycombinator.com" not in careers and norm_url(careers) not in seen:
        if page := await fetch_page(client, careers, "company", notes):
            pages.append(page)

    if company.yc_slug:
        url = YC_PAGE_URL.format(slug=company.yc_slug)
        try:
            resp = await client.get(url)
            pages += yc_pages(resp.text, url)
        except FetchError as e:
            notes.append(f"{url}: {e.label}")

    try:
        search = await client.get_json(HN_SEARCH_URL.format(q=quote(company.name)))
        pages += hn_pages(search, company.name, domain)
    except FetchError as e:
        notes.append(f"HN search: {e.label}")

    pages += hn_hiring_pages(conn, company.id)

    unique, seen = [], set()
    for p in pages:  # the same URL twice (e.g. careers page linked from the homepage) -> keep the first
        if norm_url(p.url) not in seen:
            seen.add(norm_url(p.url))
            unique.append(p)
    return unique, notes


# ---------- LLM brief ----------


def pages_text(pages: list[Page]) -> str:
    return "\n\n".join(f"=== SOURCE: {p.url} ({p.kind}) ===\n{p.text}" for p in pages)


def clean_brief(brief: CompanyBrief, pages: list[Page]) -> tuple[CompanyBrief, int]:
    """Code checks that don't trust the LLM: every fact must cite a page we actually read, and
    every person must be named on the page they're cited from. Returns (brief, how many dropped)."""
    by_url = {norm_url(p.url): p for p in pages}
    dropped = 0
    for section in ("what_they_build", "who_its_for", "tech_stack", "recent_updates", "hiring_areas"):
        kept = []
        for f in getattr(brief, section):
            page = by_url.get(norm_url(f.source_url or ""))
            if page and f.text.strip():
                f.source_url = page.url  # the exact URL as we fetched it
                kept.append(f)
            else:
                dropped += 1
        setattr(brief, section, kept)
    people = []
    for person in brief.people:
        page = by_url.get(norm_url(person.source_url or ""))
        if page and person.name.strip() and person.name.lower() in page.text.lower():
            person.source_url = page.url
            people.append(person)
        else:
            dropped += 1
    brief.people = people
    return brief, dropped


def is_fresh(fetched_at: str, max_age_days: int = cfg.BRIEF_MAX_AGE_DAYS) -> bool:
    return datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at) < timedelta(days=max_age_days)


async def research_company(conn, client, provider, company_id: int, refresh: bool = False) -> dict:
    """Returns {"brief", "pages", "cached", "dropped", "notes", "usage"}. Uses the saved brief when
    it's younger than BRIEF_MAX_AGE_DAYS (unless refresh=True)."""
    usage = {"llm_calls": 0, "prompt_tokens": 0, "output_tokens": 0}
    saved = db.get_brief(conn, company_id)
    if saved and not refresh and is_fresh(saved["fetched_at"]):
        return {
            "brief": CompanyBrief.model_validate_json(saved["brief_json"]),
            "pages": [Page(**p) for p in json.loads(saved["pages_json"])],
            "cached": True, "dropped": 0, "notes": [], "usage": usage, "fetched_at": saved["fetched_at"],
        }

    company = db.get_company(conn, company_id)
    if company is None:
        raise ValueError(f"no company #{company_id}")
    pages, notes = await gather_pages(client, conn, company)
    if not pages:
        raise ValueError(f"could not read any public page about {company.name} ({'; '.join(notes) or 'no website'})")

    prompt = fill(load_prompt("brief.md"), company=company.name, website=company.website or "unknown",
                  pages=pages_text(pages))
    brief, used = await provider.generate(prompt, CompanyBrief)
    usage = {"llm_calls": 1, "prompt_tokens": used.get("prompt_tokens", 0), "output_tokens": used.get("output_tokens", 0)}
    brief, dropped = clean_brief(brief, pages)

    model = getattr(provider, "model", "unknown")
    pages_json = json.dumps([p.model_dump() for p in pages], ensure_ascii=False)
    db.save_brief(conn, company_id, brief.model_dump_json(), pages_json, model)
    return {"brief": brief, "pages": pages, "cached": False, "dropped": dropped, "notes": notes, "usage": usage,
            "fetched_at": db.get_brief(conn, company_id)["fetched_at"]}
