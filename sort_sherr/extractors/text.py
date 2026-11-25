"""Small helpers shared by extractors."""

import html as html_lib
from datetime import datetime, timezone

from selectolax.lexbor import LexborHTMLParser as HTMLParser


def html_to_text(html: str | None) -> str | None:
    """'&lt;p&gt;Hi&lt;/p&gt;' or '<p>Hi</p>' -> 'Hi' (keeps line breaks between blocks)."""
    if not html:
        return None
    if "&lt;" in html:  # Greenhouse sends HTML-escaped HTML
        html = html_lib.unescape(html)
    text = HTMLParser(html).text(separator="\n", strip=True)
    return text or None


def parse_date(value) -> datetime | None:
    """Accepts ISO strings ('2026-11-03T18:56:53Z', '2026-05-29') or epoch milliseconds (Lever)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
