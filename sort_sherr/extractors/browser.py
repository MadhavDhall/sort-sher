"""Open a page in a real (headless) Chrome so its JavaScript runs, and return the final HTML.

Many careers pages load the job list with JavaScript after the page opens (Rootly, Spade,
Luminal, Dover boards...). A plain HTTP request only gets the empty shell; the browser gets
what a human sees. Slower (a few seconds per page), so it's only used as a fallback.

Optional: needs `pip install -e ".[browser]"` and `playwright install chromium`.
"""

import asyncio

from .. import config
from ..http import is_blocked_domain

MAX_PARALLEL_BROWSERS = 2  # each one uses a few hundred MB of RAM
_slots = asyncio.Semaphore(MAX_PARALLEL_BROWSERS)


def available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


async def render(url: str) -> str | None:
    """Final HTML of the page (plus the HTML of any embedded iframes, where job widgets often
    live). None if Playwright isn't installed or the page couldn't be opened."""
    from urllib.parse import urlparse

    if not available() or is_blocked_domain(urlparse(url).hostname or ""):
        return None
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    async with _slots:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    page = await browser.new_page(user_agent=config.USER_AGENT)
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    try:  # wait until the network is quiet (JS finished loading data)...
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except PlaywrightError:
                        pass  # ...but some sites never go quiet; use what we have
                    parts = [await page.content()]
                    for frame in page.frames[1:]:  # embedded job boards are often iframes
                        try:
                            parts.append(f"<!-- iframe {frame.url} -->" + await frame.content())
                        except PlaywrightError:
                            continue
                    return "\n".join(parts)
                finally:
                    await browser.close()
        except PlaywrightError:
            return None
