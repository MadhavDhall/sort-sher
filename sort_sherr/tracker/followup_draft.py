"""Step C: a follow-up DRAFT that adds one new piece of value (never just "bumping this").

The new value is either
  - something you give with `--value` ("I shipped X since", "I read your launch post and tried Y"),
    added as a new student item, or
  - a company fact the first email did not use, newest first (a launch, a release...): research
    refreshes the brief when it is older than BRIEF_MAX_AGE_DAYS, so recent news shows up.
Then Part 3's machinery: the LLM writes it citing F/S ids, placeholders are filled locally, and the
same checks run (rules + LLM grounding check + lint in follow-up mode). One rewrite, then needs_review.
Saved in email_drafts with followup_number 1 or 2 and the outreach id. Nothing is sent.
"""

import json
from datetime import date

from pydantic import BaseModel, Field

from .. import outreach_config, tracker_config as cfg
from ..matching.models import CandidateProfile
from ..matching.resume import redact
from ..outreach import fill, load_prompt
from ..outreach.draft import (add_usage, facts_text, fill_placeholders, grounding_problems, items_text,
                              rule_problems, student_items)
from ..outreach.lint import hard_problems, lint
from ..outreach.models import EmailVariant, numbered_facts
from ..storage import db
from .followups import followups_sent, local_date
from .lifecycle import need_outreach

ASK = "A short conversation about an internship (repeating the first email's ask)."


class FollowUpEmail(BaseModel):
    """The LLM's answer. No subject: it is fixed to "Re: <first email's subject>" (same thread)."""
    body: str = Field(description="plain text, 25-60 words, ends with [NAME]")
    facts_used: list[str] = Field(description="ids like F2 of every company fact the email mentions")
    student_items_used: list[str] = Field(description="ids like S1 of every student item the email mentions")


def pick_new_value(facts: list[dict], items: list[dict], first_draft: dict, value: str | None,
                   pii: dict) -> tuple[str, str, list[dict]]:
    """Returns (id, text, items). With --value it becomes a new S item (redacted like the resume)."""
    if value:
        text, _ = redact(" ".join(value.split()), name=pii.get("name"))
        new = {"id": f"S{len(items) + 1}", "text": f"New since the first email: {text}"}
        return new["id"], new["text"], [*items, new]
    used = {f["text"] for f in first_draft["facts_used"]}
    fresh = [f for f in facts if f["text"] not in used]
    # newest kind of fact first: recent updates (dated ones before undated), then the rest
    fresh.sort(key=lambda f: (f["section"] != "recent_updates", not f.get("date")))
    if not fresh:
        raise ValueError("nothing new to say: the first email used every fact we have. "
                         "Give one with --value \"what you built / tried since\"")
    return fresh[0]["id"], fresh[0]["text"] + (f" ({fresh[0]['date']})" if fresh[0].get("date") else ""), items


async def draft_followup(conn, client, provider, outreach_id: int, value: str | None = None,
                         redo: bool = False) -> tuple[dict, dict]:
    """Write follow-up N for this outreach. Returns (saved draft, usage)."""
    from ..outreach.research import research_company
    from ..outreach.run import drafts_left_today

    o = need_outreach(conn, outreach_id)
    if o["status"] != "sent":
        raise ValueError(f"{o['company_name']} is '{o['status']}': follow-ups are only for emails still waiting for a reply")
    events = db.outreach_events(conn, outreach_id)
    number = len(followups_sent(events)) + 1
    if number > cfg.MAX_FOLLOWUPS:
        raise ValueError(f"already sent {cfg.MAX_FOLLOWUPS} follow-ups to {o['company_name']}: that's the limit")
    existing = [d for d in db.followup_drafts(conn, outreach_id) if d["followup_number"] == number]
    if existing and not redo:
        raise ValueError(f"follow-up {number} is already drafted (draft #{existing[-1]['id']}); use --redo to write it again")
    if drafts_left_today(conn, o["candidate_id"]) <= 0:
        raise ValueError(f"daily cap of {outreach_config.DAILY_DRAFT_CAP} drafts reached (follow-ups count too); try tomorrow")
    first = db.get_draft(conn, o["draft_id"]) if o["draft_id"] else None
    if first is None:
        raise ValueError(f"outreach #{outreach_id} has no first-email draft to follow up on (it was logged by hand)")

    usage = {"llm_calls": 0, "prompt_tokens": 0, "output_tokens": 0}
    r = await research_company(conn, client, provider, o["company_id"])
    for k in usage:
        usage[k] += r["usage"].get(k, 0)
    row = db.get_candidate(conn, o["candidate_id"])
    profile, pii = CandidateProfile.model_validate_json(row["profile_json"]), json.loads(row["pii_json"])
    facts, items = numbered_facts(r["brief"]), student_items(profile)
    new_id, new_text, items = pick_new_value(facts, items, first, value, pii)

    first_name = (o.get("contact_person") or "").split()
    greeting = f"Hi {first_name[0]}," if first_name else f"Hi {o['company_name']} team,"
    sent_at = next(e["at"] for e in events if e["kind"] == "sent")
    subject = first["subject"] if first["subject"].lower().startswith("re:") else f"Re: {first['subject']}"
    values = {"[NAME]": pii.get("name") or ""}
    sources = "\n".join([facts_text(facts), items_text(items), ASK])
    fact_ids, item_ids = {f["id"] for f in facts}, {i["id"] for i in items}

    def prompt(feedback=""):
        last = " It is the LAST one: no more emails after this." if number == cfg.MAX_FOLLOWUPS else ""
        return fill(load_prompt("followup_email.md"), days=(date.today() - local_date(sent_at)).days,
                    subject=first["subject"], first_email=first["edited_body"] or first["body"],
                    student=items_text(items), company=o["company_name"], facts=facts_text(facts),
                    new_id=new_id, new_text=new_text, number=number, max_followups=cfg.MAX_FOLLOWUPS,
                    last_note=last, greeting=greeting, feedback=feedback)

    async def write_and_check(feedback=""):
        v, used = await provider.generate(prompt(feedback), FollowUpEmail)
        add_usage(usage, used)
        email = EmailVariant(style="follow-up", subject=subject, body=v.body,
                             facts_used=v.facts_used, student_items_used=v.student_items_used)
        problems = rule_problems(email, fact_ids, item_ids, sources)
        if new_id not in [*email.facts_used, *email.student_items_used]:
            problems.append(f"does not use the new thing {new_id}: a follow-up must add it")
        found, used = await grounding_problems(provider, {"A": email}, facts, items, ASK)
        add_usage(usage, used)
        problems += found["A"]
        problems += hard_problems(lint(subject, fill_placeholders(email.body, values), followup=True))
        return email, problems

    email, problems = await write_and_check()
    if problems:  # one rewrite with the problems spelled out
        email, problems = await write_and_check(
            "\nYOUR PREVIOUS DRAFT HAD THESE PROBLEMS. Fix them:\n" + "\n".join(f"- {p}" for p in problems))

    body = fill_placeholders(email.body, values)
    by_id, item_by_id = {f["id"]: f for f in facts}, {i["id"]: i for i in items}
    if existing:
        db.delete_followup_drafts(conn, outreach_id, number)
    draft_id = db.save_draft(conn, {
        "match_id": first["match_id"], "candidate_id": o["candidate_id"], "company_id": o["company_id"],
        "variant": "A", "style": "follow-up", "subject": subject, "body": body.strip(),
        "facts_used": [by_id[x] for x in email.facts_used if x in by_id],
        "student_items_used": [item_by_id[x] for x in email.student_items_used if x in item_by_id],
        "check_status": "needs_review" if problems else "ok", "check_notes": problems,
        "lint_flags": lint(subject, body, followup=True), "outreach_id": outreach_id, "followup_number": number,
    })
    return db.get_draft(conn, draft_id), usage
