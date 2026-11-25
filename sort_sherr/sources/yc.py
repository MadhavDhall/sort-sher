"""Seed companies from the open yc-oss dataset (https://github.com/yc-oss/api).

It is a static JSON mirror of YC's public company directory, refreshed daily.
"""

from ..http import PoliteClient
from ..models import Company, domain_of

ALL_URL = "https://yc-oss.github.io/api/companies/all.json"
HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"


def to_company(raw: dict) -> Company:
    locations = [loc.strip() for loc in (raw.get("all_locations") or "").split(";") if loc.strip()]
    return Company(
        name=raw["name"].strip(),
        website=raw.get("website") or None,
        domain=domain_of(raw.get("website")),
        sources=["yc"],
        yc_slug=raw.get("slug"),
        yc_batch=raw.get("batch"),
        team_size=raw.get("team_size"),
        is_hiring=raw.get("isHiring"),
        stage=raw.get("stage"),
        top_company=raw.get("top_company"),
        locations=locations,
        description=raw.get("one_liner") or raw.get("long_description"),
        tags=raw.get("tags") or [],
    )


async def fetch_yc_companies(client: PoliteClient, hiring_only: bool = False) -> list[Company]:
    """Active YC companies only (we skip Inactive / Acquired / Public)."""
    raw_list = await client.get_json(HIRING_URL if hiring_only else ALL_URL)
    return [to_company(r) for r in raw_list if r.get("status") == "Active" and r.get("name")]
