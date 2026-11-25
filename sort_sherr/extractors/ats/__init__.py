"""Tier 1: public ATS APIs.

Each adapter module has `parse(json) -> list[JobDraft]` and either:
  - `api_url(slug)`         when one request is enough (most ATSs), or
  - `async fetch(client, slug)` when it needs more than one request (Dover).
"""

from ...http import PoliteClient
from ...models import AtsType, JobDraft
from . import ashby, dover, gem, greenhouse, lever, rippling, workable

ADAPTERS = {
    AtsType.gem: gem,
    AtsType.greenhouse: greenhouse,
    AtsType.lever: lever,
    AtsType.ashby: ashby,
    AtsType.workable: workable,
    AtsType.rippling: rippling,
    AtsType.dover: dover,
}


async def fetch_ats_jobs(client: PoliteClient, ats: AtsType, slug: str) -> list[JobDraft]:
    adapter = ADAPTERS[ats]
    if hasattr(adapter, "fetch"):
        return await adapter.fetch(client, slug)
    data = await client.get_json(adapter.api_url(slug))
    return adapter.parse(data)
