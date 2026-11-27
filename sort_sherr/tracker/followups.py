"""Step C: when to follow up, and what needs your attention today (`due`).

Rules (numbers in tracker_config.py):
  - no reply 5 business days after the first email  -> follow-up 1 is due
  - no reply 7 business days after follow-up 1 was SENT -> follow-up 2 (the last one) is due
  - no reply 7 business days after follow-up 2 was sent -> ghosted
  - any reply, bounce or final status stops all of this at once (only 'sent' outreach gets follow-ups)
Dates count from when you actually SENT the previous message, not from when it was due:
a follow-up you send late doesn't make the next one come early.
Business days are Monday-Friday in your local time; holidays are not skipped.
"""

from datetime import date, datetime, timedelta

from .. import tracker_config as cfg
from ..storage import db
from .lifecycle import FINAL


def add_business_days(start: date, n: int) -> date:
    """start + n weekdays. Saturday + 1 = Monday, Friday + 5 = next Friday."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 0-4 = Monday-Friday
            n -= 1
    return d


def local_date(iso: str) -> date:
    """A stored UTC timestamp -> the calendar day it was on YOUR clock."""
    return datetime.fromisoformat(iso).astimezone().date()


def followups_sent(events: list[dict]) -> list[dict]:
    return [e for e in events if e["kind"] == "followup_sent"]


def next_action(o: dict, events: list[dict], today: date) -> dict | None:
    """What this outreach needs next, or None. A dict: kind, label, due_on (a date), priority
    (1 = most urgent). An action is DUE when due_on <= today; until then it is only shown as "waiting"."""
    status = o["status"]
    if status in FINAL:
        return None

    if status == "drafted":
        return {"kind": "send", "label": "approved, not sent yet: send it, then log it with `sent`",
                "due_on": today, "priority": 5}

    if status == "interview":
        interview = [e for e in events if e["kind"] == "interview"][-1]
        if not interview["scheduled_for"]:
            return {"kind": "interview", "label": "interview: add its date (status <id> interview --when ...)",
                    "due_on": today, "priority": 1}
        day = local_date(interview["scheduled_for"])
        if day < today:
            return {"kind": "interview", "label": f"interview was on {day}: log the result (offer / rejected)",
                    "due_on": day, "priority": 2}
        return {"kind": "interview", "label": f"interview on {day}: prepare",
                "due_on": day - timedelta(days=cfg.INTERVIEW_SOON_DAYS), "priority": 1}

    if status == "replied":
        last = events[-1]
        if last["kind"] == "answered":
            return None  # the ball is in their court
        kind = o["reply_type"] or "neutral"
        label = {"positive": "positive reply: answer it today",
                 "neutral": "reply: answer it",
                 "negative": "negative reply: thank them, then mark it rejected"}[kind]
        return {"kind": "reply", "label": label, "due_on": local_date(last["at"]),
                "priority": 2 if kind != "negative" else 3}

    # status == "sent": waiting for a reply -> follow-ups, then ghosted
    sent = [e for e in events if e["kind"] == "sent"][0]
    done = followups_sent(events)
    last_sent = local_date((done[-1] if done else sent)["at"])
    if len(done) < cfg.MAX_FOLLOWUPS:
        n = len(done) + 1
        wait = cfg.FIRST_FOLLOWUP_DAYS if n == 1 else cfg.SECOND_FOLLOWUP_DAYS
        last = " (the last one)" if n == cfg.MAX_FOLLOWUPS else ""
        return {"kind": "followup", "number": n, "label": f"follow-up {n}{last} due",
                "due_on": add_business_days(last_sent, wait), "priority": 3}
    return {"kind": "ghost", "label": f"no reply after {cfg.MAX_FOLLOWUPS} follow-ups: ghosted",
            "due_on": add_business_days(last_sent, cfg.GHOST_AFTER_DAYS), "priority": 4}


def due_items(conn, candidate_id: int, today: date | None = None, apply_ghost: bool = False) -> list[dict]:
    """Everything that needs action today, most urgent first. Each item = the outreach dict + "action"
    (+ "days_late"). apply_ghost=True (the `due` command) marks overdue silent outreach as ghosted."""
    today = today or date.today()
    events = db.candidate_events(conn, candidate_id)
    items = []
    for o in db.candidate_outreach(conn, candidate_id):
        action = next_action(o, events.get(o["id"], []), today)
        if action is None or action["due_on"] > today:
            continue
        if action["kind"] == "ghost" and apply_ghost:
            db.record_event(conn, o["id"], "ghosted", db.now(), source="auto",
                            note=f"no reply after {cfg.MAX_FOLLOWUPS} follow-ups", status="ghosted")
            action = {**action, "label": "marked ghosted (no reply after the last follow-up)"}
        if action["kind"] == "followup":
            ready = [d for d in db.followup_drafts(conn, o["id"]) if d["followup_number"] == action["number"]]
            action["draft_id"] = next((d["id"] for d in ready if d["chosen"]), ready[-1]["id"] if ready else None)
        items.append({**o, "action": action, "days_late": (today - action["due_on"]).days})
    items.sort(key=lambda i: (i["action"]["priority"], i["action"]["due_on"], i["id"]))
    return items
