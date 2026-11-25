"""Try the tiers in order and stop at the first one that gives an answer.

  Tier 1: ATS public API      (most reliable)
  Tier 2: JSON-LD on the page (structured, but only some sites have it)
  Tier 3: LLM on page text    (works almost anywhere, free tier is slow, can be wrong)

If the plain page gives no jobs, we open it in a real browser (Playwright) so its JavaScript
runs, and try again on what a human would see. That rendered page often reveals an ATS widget
(-> Tier 1) or a job list the plain HTML didn't have (-> Tier 2/3).
"""

from dataclasses import dataclass, field

from ..discovery.ats import detect_ats
from ..http import FetchError, PoliteClient
from ..models import AtsType, Company, JobDraft
from . import browser, jsonld, llm
from .ats import fetch_ats_jobs
from .llm import LLMProvider


@dataclass
class CrawlResult:
    status: str  # "ok", "no_jobs", or a failure label
    tier: int | None = None
    jobs: list[JobDraft] = field(default_factory=list)
    error: str | None = None
    found_ats: tuple[AtsType, str] | None = None  # an ATS we only spotted while crawling


async def extract_jobs(client: PoliteClient, company: Company, conn=None, provider: LLMProvider | None = None) -> CrawlResult:
    """`conn` is used for the LLM cache; `provider` None means tier 3 is skipped."""
    notes = []

    # ---- Tier 1: ATS found during discover ----
    if company.ats_type and company.ats_slug:
        try:
            jobs = await fetch_ats_jobs(client, company.ats_type, company.ats_slug)
            # An ATS board is the source of truth: an empty board really means "no open roles".
            return CrawlResult("ok" if jobs else "no_jobs", tier=1, jobs=jobs)
        except FetchError as e:
            notes.append(f"tier1 {e.label}")
        except (ValueError, KeyError) as e:
            notes.append(f"tier1 bad_response {type(e).__name__}")

    if not company.careers_url:
        return CrawlResult("no_careers_page", error="; ".join(notes) or None)

    # ---- Plain page first (fast) ----
    try:
        page = await client.get(company.careers_url)
        page_url = str(page.url)
        result = await from_page(client, conn, provider, page.text, page_url, notes)
        if result.jobs or result.status not in ("no_jobs", "tier3_skipped"):
            return result  # found jobs, or a real error (e.g. LLM failed) -- don't retry
    except FetchError as e:  # e.g. the site blocks plain HTTP clients but works in a browser
        page_url = company.careers_url
        notes.append(f"careers page {e.label}")
        result = CrawlResult(f"careers_{e.label}", error="; ".join(notes))

    # ---- Then the page as a browser sees it (JavaScript run) ----
    if not browser.available():
        return result
    rendered = await browser.render(page_url)
    if not rendered:
        notes.append("browser could not open page")
        result.error = "; ".join(notes)
        return result
    notes.append("rendered with browser")
    rendered_result = await from_page(client, conn, provider, rendered, page_url, notes)
    if rendered_result.jobs or rendered_result.status == "tier3_failed":
        return rendered_result
    rendered_result.error = "; ".join(notes) or None

    # ---- Last resort for YC companies: their public YC jobs page (has JSON-LD) ----
    yc_url = f"https://www.ycombinator.com/companies/{company.yc_slug}/jobs" if company.yc_slug else None
    if yc_url and "ycombinator.com" not in page_url:
        try:
            yc_page = await client.get(yc_url)
            jobs = await jsonld.extract_jobs(client, yc_page.text, str(yc_page.url))
            if jobs:
                notes.append("found on YC jobs page")
                return CrawlResult("ok", tier=2, jobs=jobs, error="; ".join(notes))
        except FetchError:
            pass
    return rendered_result


async def from_page(client, conn, provider, html: str, page_url: str, notes: list[str]) -> CrawlResult:
    """Tiers 1-3 on one version of the careers page (plain or rendered)."""
    # Tier 1 again: an ATS link/iframe inside this page (common once JavaScript has run)
    if found := detect_ats(html, page_url):
        try:
            jobs = await fetch_ats_jobs(client, *found)
            return CrawlResult("ok" if jobs else "no_jobs", tier=1, jobs=jobs, found_ats=found)
        except (FetchError, ValueError, KeyError) as e:
            notes.append(f"tier1 {found[0].value}/{found[1]} failed: {type(e).__name__}")

    # Tier 2: JSON-LD
    jobs = await jsonld.extract_jobs(client, html, page_url)
    if jobs:
        return CrawlResult("ok", tier=2, jobs=jobs)
    notes.append("tier2 no JobPosting JSON-LD")

    # Tier 3: LLM
    if provider is None:
        return CrawlResult("tier3_skipped", error="; ".join(notes + ["no GEMINI_API_KEY"]))
    try:
        jobs, llm_notes = await llm.extract_jobs(conn, provider, html, page_url)
    except Exception as e:  # any provider error (auth, rate limit, bad output) is a per-company failure
        return CrawlResult("tier3_failed", error="; ".join(notes + [f"{type(e).__name__}: {e}"[:300]]))
    notes.extend(llm_notes)
    # Unlike an ATS board, an empty LLM answer is a weak signal, but still the best we have.
    return CrawlResult("ok" if jobs else "no_jobs", tier=3, jobs=jobs, error="; ".join(notes) or None)
