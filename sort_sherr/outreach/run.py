"""One `draft` run: the candidate's top matches -> research -> contacts -> two drafts each.

Guardrails (quality over quantity, never spam):
  - at most DAILY_DRAFT_CAP companies get new drafts per day, per candidate
  - a company you already wrote to (Part 4 outreach with a 'sent' event) is never drafted again
  - follow-up drafts (Part 4) count toward the same daily cap
  - a company that already has drafts is skipped unless redo=True
  - nothing is sent: drafts wait in the database for `review`
"""

import json
from datetime import datetime, time, timezone

from .. import outreach_config as cfg
from ..matching.models import CandidateProfile, Preferences
from ..matching.rerank import usage_cost
from ..storage import db
from .contacts import default_recipient, find_contacts, has_mx
from .draft import draft_company
from .models import numbered_facts
from .research import research_company

SETUPS = {"remote": "remote", "onsite": "onsite", "either": "open to remote or onsite"}


def start_of_today() -> str:
    """Local midnight, as the same UTC ISO text the database stores (so string comparison works)."""
    midnight = datetime.combine(datetime.now().date(), time.min).astimezone()
    return midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def drafts_left_today(conn, candidate_id: int) -> int:
    """New companies drafted today AND Part 4 follow-up drafts written today share one cap."""
    since = start_of_today()
    used = db.companies_drafted_since(conn, candidate_id, since) + db.followups_drafted_since(conn, candidate_id, since)
    return max(0, cfg.DAILY_DRAFT_CAP - used)


def build_ask(match: dict, window: str, setup: str, locations: list[str]) -> dict:
    """What the email asks for: the best posted role if it IS an internship, else the internship
    Part 2 suggested pitching (a student shouldn't ask for "Member of Technical Staff")."""
    posted_intern = match.get("job_title") if match.get("job_is_internship") == 1 else None
    role = posted_intern or match.get("pitch_role") or "Software engineering intern"
    setup_text = SETUPS[setup]
    if setup == "onsite" and locations:
        setup_text += f" ({', '.join(locations)})"
    return {"role": role, "window": window, "setup": setup_text}


async def draft_for_candidate(conn, client, provider, candidate_id: int, *, top: int, window: str, setup: str,
                              run_id: int | None = None, redo: bool = False, finder=None, mx_check=has_mx,
                              progress=print) -> dict:
    """Returns {"run_id", "results": [{company, status, ...}], "usage"}."""
    row = db.get_candidate(conn, candidate_id)
    if row is None:
        raise ValueError(f"no candidate #{candidate_id}; run parse-resume first")
    profile = CandidateProfile.model_validate_json(row["profile_json"])
    pii = json.loads(row["pii_json"])
    run_id = run_id or db.latest_run_id(conn, candidate_id, reranked_only=True)
    if not run_id:
        raise ValueError("no reranked match run for this candidate; run `match` first")
    prefs = Preferences.model_validate_json(db.get_run(conn, run_id)["prefs_json"])

    contacted = db.contacted_company_ids(conn, candidate_id)
    drafted = {d["company_id"] for d in db.candidate_drafts(conn, candidate_id)}
    left = drafts_left_today(conn, candidate_id)
    usage = {"llm_calls": 0, "prompt_tokens": 0, "output_tokens": 0}
    results = []

    def add(used):
        for k in usage:
            usage[k] += used.get(k, 0)

    try:
        for n, m in enumerate(db.run_matches(conn, run_id)[:top], 1):
            cid, name = m["company_id"], m["company_name"]
            if cid in contacted:
                results.append({"company": name, "company_id": cid, "status": "skipped: already contacted"})
                continue
            if cid in drafted and not redo:
                results.append({"company": name, "company_id": cid, "status": "skipped: already drafted (use --redo)"})
                continue
            if left <= 0:
                results.append({"company": name, "company_id": cid, "status": f"skipped: daily cap of {cfg.DAILY_DRAFT_CAP} reached"})
                continue

            progress(f"[{n}/{top}] {name}: researching...")
            try:
                r = await research_company(conn, client, provider, cid)
                add(r["usage"])
                contacts = await find_contacts(conn, client, cid, r["pages"], r["brief"], apply_url=m["apply_url"],
                                               finder=finder, mx_check=mx_check)
                recipient = default_recipient(contacts)
                others = [(d["company_name"], d["edited_body"] or d["body"])
                          for d in db.candidate_drafts(conn, candidate_id) if d["company_id"] != cid]
                progress(f"        {len(r['pages'])} pages, {len(numbered_facts(r['brief']))} facts, "
                         f"{len(contacts)} contacts; drafting...")
                drafts, used = await draft_company(
                    provider, profile=profile, pii=pii, company_name=name, brief=r["brief"], match=m,
                    ask=build_ask(m, window, setup, prefs.locations), recipient=recipient, other_drafts=others,
                )
                add(used)
            except ValueError as e:
                results.append({"company": name, "company_id": cid, "status": f"skipped: {e}"})
                continue
            if redo:
                db.delete_drafts(conn, candidate_id, cid)
            ids = [db.save_draft(conn, {"match_id": m["id"], "candidate_id": candidate_id, "company_id": cid, **d})
                   for d in drafts]
            left -= 1
            results.append({"company": name, "company_id": cid, "status": "drafted", "draft_ids": ids,
                            "checks": [d["check_status"] for d in drafts]})
    finally:  # log tokens even when Gemini fails halfway (everything finished so far is cached)
        db.log_outreach_run(conn, "draft", candidate_id, usage, usage_cost(usage))
    return {"run_id": run_id, "results": results, "usage": usage}
