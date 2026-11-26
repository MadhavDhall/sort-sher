"""Tier 3: let an LLM read the careers page and fill in our job schema.

Flow:  HTML -> page text with [links](url) -> hash -> cache hit? return cached
                                                   else LLM structured output -> cache -> return
If the page looks JS-rendered (almost no text) and Playwright is installed, we render it first.

The LLM sits behind a tiny `LLMProvider` interface. We use Google Gemini's free tier;
switching to another provider means writing one class with one method.
"""

import asyncio
import hashlib
import time
from typing import Protocol
from urllib.parse import urljoin

from pydantic import BaseModel, Field
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .. import config
from ..models import JobDraft, guess_is_internship
from ..storage import db

MIN_TEXT_CHARS = 300  # less visible text than this -> probably rendered by JavaScript
MAX_MARKDOWN_CHARS = 100_000  # cost guard; we record a note when we cut a page
RETRY_CODES = {429, 500, 503}  # temporary errors worth waiting for


# ---------- what we ask the LLM to return ----------


class LLMJob(BaseModel):
    title: str
    team: str | None = None
    location: str | None = None
    remote: bool | None = Field(None, description="true/false only if the page says so, else null")
    is_internship: bool | None = Field(None, description="true for intern/co-op roles, null if unclear")
    employment_type: str | None = None
    description: str | None = Field(None, description="1-3 sentence summary, only from the page")
    requirements: str | None = None
    apply_url: str | None = Field(None, description="absolute URL to the posting or application, if on the page")


class LLMJobList(BaseModel):
    jobs: list[LLMJob]


PROMPT = """Below is a company's careers page converted to markdown.
List every open job posting on it. Rules:
- Only include roles that are actually listed. Never invent jobs, links, or details.
- If the page lists no specific roles (e.g. only "send us your CV"), return an empty list.
- Use null for anything the page doesn't state.

Page URL: {url}

{markdown}"""


# ---------- provider interface ----------


class LLMProvider(Protocol):
    """Anything with these methods can be our LLM (Gemini in real runs, a fake in tests).

    - extract_jobs: Part 1, Tier 3
    - generate: Part 2 (resume parsing, reranking); returns (answer, token usage)
    """

    async def extract_jobs(self, markdown: str, page_url: str) -> list[LLMJob]: ...

    async def generate(self, prompt: str, schema: type[BaseModel]) -> tuple[BaseModel, dict]: ...


class GeminiProvider:
    """Google Gemini, using the FREE tier (key from aistudio.google.com, no credit card).

    The free tier only allows a few requests per minute, so we:
      - send one request at a time, at least LLM_MIN_INTERVAL seconds apart
      - on a 429 ("too many requests"), wait and try again a few times
    """

    def __init__(self, api_key: str, model: str = config.LLM_MODEL):
        from google import genai  # imported here so the rest of the app works without it

        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.lock = asyncio.Lock()
        self.last_call = 0.0

    async def generate(self, prompt: str, schema: type[BaseModel]) -> tuple[BaseModel, dict]:
        """Send one prompt; Gemini must answer as JSON in the shape of `schema`.
        Returns (parsed answer, {"prompt_tokens": .., "output_tokens": ..})."""
        from google.genai import errors, types

        request_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,  # Gemini must answer in exactly this shape
            temperature=0,  # extraction/scoring, not creativity
        )
        async with self.lock:  # one LLM call at a time
            for attempt in range(4):
                wait = config.LLM_MIN_INTERVAL - (time.monotonic() - self.last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    response = await self.client.aio.models.generate_content(
                        model=self.model, contents=prompt, config=request_config
                    )
                    break
                except errors.APIError as e:
                    # 429 = we hit the free-tier limit; 500/503 = Google is overloaded right now.
                    # Both are temporary, so wait 30s, 60s, 90s and try again.
                    if e.code not in RETRY_CODES or attempt == 3:
                        raise
                    await asyncio.sleep(30 * (attempt + 1))
                finally:
                    self.last_call = time.monotonic()

        parsed = response.parsed
        if parsed is None:  # the SDK couldn't parse it for us -> try the raw text ourselves
            parsed = schema.model_validate_json(response.text or "")
        meta = getattr(response, "usage_metadata", None)
        usage = {
            "prompt_tokens": getattr(meta, "prompt_token_count", 0) or 0,
            "output_tokens": getattr(meta, "candidates_token_count", 0) or 0,
        }
        return parsed, usage

    async def extract_jobs(self, markdown: str, page_url: str) -> list[LLMJob]:
        parsed, _ = await self.generate(PROMPT.format(url=page_url, markdown=markdown), LLMJobList)
        return parsed.jobs


def default_provider() -> LLMProvider | None:
    """None when no API key is configured -> tier 3 is skipped."""
    return GeminiProvider(config.GEMINI_API_KEY) if config.GEMINI_API_KEY else None


# ---------- page -> markdown ----------


def html_to_markdown(html: str, page_url: str = "") -> str:
    """Visible text of the page, with links kept as [text](url) so the LLM can return apply URLs.

    Why not trafilatura? It is built for articles and drops "boilerplate" -- which on a careers
    page is exactly the job list. (On real pages it kept the intro text and lost every role.)
    """
    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript, svg, template, iframe"):
        tag.decompose()
    for a in tree.css("a[href]"):
        text = a.text(separator=" ", strip=True)
        href = urljoin(page_url, a.attributes.get("href") or "")
        if text and href.startswith("http"):
            a.replace_with(f" [{text}]({href}) ")
    body = tree.body.text(separator="\n", strip=True) if tree.body else ""
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return "\n".join(line for line in lines if line)


# ---------- tier 3 entry point ----------


def to_job_draft(job: LLMJob) -> JobDraft:
    data = job.model_dump()
    if data["is_internship"] is None:
        data["is_internship"] = guess_is_internship(job.title, job.employment_type) or None
    return JobDraft(**data)


async def extract_jobs(conn, provider: LLMProvider, html: str, page_url: str) -> tuple[list[JobDraft], list[str]]:
    """Returns (jobs, notes). Notes explain anything unusual (almost empty page, page cut, cache hit).
    (Rendering JavaScript pages happens before this, in pipeline.py / browser.py.)"""
    notes = []
    markdown = html_to_markdown(html, page_url)
    if len(markdown) < MIN_TEXT_CHARS:
        notes.append("page has almost no text")
        return [], notes  # nothing for the LLM to read -- don't waste a free-tier request
    if len(markdown) > MAX_MARKDOWN_CHARS:
        notes.append(f"page cut from {len(markdown)} to {MAX_MARKDOWN_CHARS} chars")
        markdown = markdown[:MAX_MARKDOWN_CHARS]

    content_hash = hashlib.sha256(f"{page_url}\n{markdown}".encode()).hexdigest()
    cached = db.cache_get(conn, content_hash)
    if cached is not None:
        notes.append("llm cache hit")
        return cached, notes

    jobs = [to_job_draft(j) for j in await provider.extract_jobs(markdown, page_url)]
    db.cache_put(conn, content_hash, jobs)
    return jobs, notes
