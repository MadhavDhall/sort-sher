"""One polite async HTTP client used by everything.

Every request goes through `PoliteClient.get()`, which:
  1. refuses blocked domains (LinkedIn, Wellfound, ...) -- always, their terms forbid scraping
  2. checks robots.txt for that host, ONLY if config.RESPECT_ROBOTS is on (off by default)
  3. waits so the same host is hit at most once per PER_DOMAIN_DELAY (or robots Crawl-delay)
  4. limits total parallel requests (MAX_CONCURRENCY)
  5. retries 429 / 5xx / network errors with exponential backoff
Failures raise FetchError with a short `label` that we store and show in `stats`.
"""

import asyncio
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

from . import config


class FetchError(Exception):
    def __init__(self, label: str, detail: str = ""):
        super().__init__(f"{label}: {detail}" if detail else label)
        self.label = label  # e.g. "robots_blocked", "http_404", "timeout"
        self.detail = detail


def is_blocked_domain(host: str) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in config.BLOCKED_DOMAINS)


class PoliteClient:
    def __init__(self, max_concurrency: int = config.MAX_CONCURRENCY):
        self.client = httpx.AsyncClient(
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.TIMEOUT,
            follow_redirects=True,
        )
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.host_locks: dict[str, asyncio.Lock] = {}
        self.last_hit: dict[str, float] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    # ---- public ----

    async def get(self, url: str) -> httpx.Response:
        host = urlparse(url).hostname or ""
        if is_blocked_domain(host):
            raise FetchError("blocked_domain", host)
        if config.RESPECT_ROBOTS and not await self.allowed_by_robots(url):
            raise FetchError("robots_blocked", url)

        for attempt in range(config.MAX_RETRIES + 1):
            try:
                resp = await self._polite_request(url)
            except httpx.TimeoutException:
                error = FetchError("timeout", url)
            except httpx.HTTPError as e:
                error = FetchError("network_error", f"{type(e).__name__}: {url}")
            else:
                if is_blocked_domain(resp.url.host or ""):
                    raise FetchError("blocked_domain", f"redirected to {resp.url.host}")
                if resp.status_code < 400:
                    return resp
                error = FetchError(f"http_{resp.status_code}", url)
                if resp.status_code != 429 and resp.status_code < 500:
                    raise error  # 404, 403, ... retrying won't help
                retry_after = resp.headers.get("retry-after", "")
                if retry_after.isdigit():
                    await asyncio.sleep(min(int(retry_after), 60))

            if attempt < config.MAX_RETRIES:
                await asyncio.sleep(2**attempt)  # 1s, 2s, 4s
        raise error

    async def get_json(self, url: str):
        return (await self.get(url)).json()

    # ---- internals ----

    async def _polite_request(self, url: str) -> httpx.Response:
        host = urlparse(url).hostname or ""
        lock = self.host_locks.setdefault(host, asyncio.Lock())
        async with lock:  # one request at a time per host...
            wait = self._delay_for(host) - (time.monotonic() - self.last_hit.get(host, 0))
            if wait > 0:
                await asyncio.sleep(wait)  # ...spaced out by the delay
            async with self.semaphore:  # ...and a global cap on parallel requests
                try:
                    return await self.client.get(url)
                finally:
                    self.last_hit[host] = time.monotonic()

    def _delay_for(self, host: str) -> float:
        rp = self.robots.get(host)
        crawl_delay = rp.crawl_delay(config.USER_AGENT) if rp else None
        return max(config.PER_DOMAIN_DELAY, float(crawl_delay or 0))

    async def allowed_by_robots(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host not in self.robots:
            self.robots[host] = await self._load_robots(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        rp = self.robots[host]
        return True if rp is None else rp.can_fetch(config.USER_AGENT, url)

    async def _load_robots(self, robots_url: str) -> urllib.robotparser.RobotFileParser | None:
        """None means "no rules, everything allowed"."""
        rp = urllib.robotparser.RobotFileParser()
        try:
            async with self.semaphore:
                resp = await self.client.get(robots_url)
        except httpx.HTTPError:
            return None  # can't reach robots.txt; the real request will fail on its own if the site is down
        if resp.status_code >= 500:
            rp.parse(["User-agent: *", "Disallow: /"])  # standard says: server error -> assume disallowed
            return rp
        if resp.status_code >= 400 or "html" in resp.headers.get("content-type", ""):
            return None  # no robots.txt (404) -> everything allowed
        rp.parse(resp.text.splitlines())
        return rp
