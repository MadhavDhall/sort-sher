"""Step A + B: the outreach lifecycle, and logging what happened (by hand: works for every channel).

    drafted -> sent -> replied (positive / neutral / negative) -> interview -> offer / rejected
                  \\-> bounced       \\-> ghosted (no reply after the follow-ups)   any -> withdrawn

Every change is an EVENT with a timestamp (outreach_events); outreach.status is just the latest.
Two events don't change the status: followup_sent (1 or 2) and answered (you replied to them).

Nothing here sends anything. You send the email yourself, then tell Sort Sherr you did.
"""

import re
from datetime import datetime, timedelta, timezone

from ..storage import db

CHANNELS = ("email", "linkedin", "x", "apply_form")
REPLY_TYPES = ("positive", "neutral", "negative")
# Statuses you set with `status` (sent and replied have their own commands), plus the "answered" event.
MANUAL_STATUSES = ("interview", "offer", "rejected", "ghosted", "bounced", "withdrawn", "answered")
FINAL = {"offer", "rejected", "ghosted", "bounced", "withdrawn"}  # nothing more to do

# Which status may come next. A reply must be logged before an interview or a rejection, so
# "did they reply?" is always one clear event. A late reply can revive a ghosted outreach.
NEXT = {
    "drafted": {"sent", "withdrawn"},
    "sent": {"replied", "bounced", "ghosted", "withdrawn"},
    "replied": {"replied", "interview", "rejected", "offer", "ghosted", "withdrawn"},
    "interview": {"interview", "offer", "rejected", "ghosted", "withdrawn"},  # interview again = next round
    "ghosted": {"replied"},
    "offer": set(), "rejected": set(), "bounced": set(), "withdrawn": set(),
}

# Contact type (Part 3) -> the channel you most likely used for it
CHANNEL_FOR_CONTACT = {"email": "email", "linkedin": "linkedin", "x": "x", "apply_url": "apply_form"}

WHEN = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{1,2}:\d{2})?$")


def parse_when(text: str | None, allow_future: bool = False) -> str:
    """'2026-11-25' or '2026-11-25 14:30' in YOUR local time -> the UTC ISO text the database stores.
    Empty = now. Sends and replies can't be in the future; an interview time can."""
    if not text:
        return db.now()
    text = " ".join(text.split())
    if not WHEN.match(text):
        raise ValueError(f"use YYYY-MM-DD or 'YYYY-MM-DD HH:MM', not {text!r}")
    local = datetime.strptime(text, "%Y-%m-%d %H:%M" if " " in text else "%Y-%m-%d")
    if " " not in text:
        local = local.replace(hour=9)  # a date alone: 9:00, so it lands on that day in any time zone
    at = local.astimezone(timezone.utc)  # naive -> treated as local time
    if not allow_future and at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError(f"{text} is in the future")
    return at.isoformat(timespec="seconds")


def need_outreach(conn, outreach_id: int) -> dict:
    o = db.get_outreach(conn, outreach_id)
    if o is None:
        raise ValueError(f"no outreach #{outreach_id} (see `outreach --candidate <id>`)")
    return o


def allowed_statuses(status: str) -> list[str]:
    """What `status` may set next from here (for the website's dropdown)."""
    out = [s for s in MANUAL_STATUSES if s in NEXT[status]]
    return out + (["answered"] if status in ("replied", "interview") else [])


def check_move(o: dict, new: str) -> None:
    if new not in NEXT[o["status"]]:
        hint = "; log the reply first: reply <id> --type ..." if o["status"] == "sent" and new in ("interview", "rejected", "offer") else ""
        raise ValueError(f"outreach #{o['id']} ({o['company_name']}) is '{o['status']}': can't go to '{new}'{hint}")


def log_sent(conn, draft_id: int, channel: str | None = None, at: str | None = None,
             contact_id: int | None = None) -> tuple[int, str]:
    """You sent this draft. A first email -> 'sent'. A follow-up draft -> a 'followup_sent' event.
    channel: defaults to the contact's kind (email address -> email, LinkedIn profile -> linkedin...).
    Returns (outreach id, what was logged)."""
    from ..outreach.contacts import default_recipient

    d = db.get_draft(conn, draft_id)
    if d is None:
        raise ValueError(f"no draft #{draft_id}")
    if channel is not None and channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    at = parse_when(at)

    if d["followup_number"] > 0:  # a follow-up in an existing thread
        o = need_outreach(conn, d["outreach_id"])
        if o["status"] != "sent":
            raise ValueError(f"{o['company_name']} is '{o['status']}': follow-ups only go out while waiting for a reply")
        if any(e["kind"] == "followup_sent" and e["followup_number"] == d["followup_number"]
               for e in db.outreach_events(conn, o["id"])):
            raise ValueError(f"follow-up {d['followup_number']} to {o['company_name']} is already logged")
        db.record_event(conn, o["id"], "followup_sent", at, followup_number=d["followup_number"])
        return o["id"], f"follow-up {d['followup_number']}"

    contacts = db.company_contacts(conn, d["company_id"])
    if contact_id is not None and contact_id not in {c["id"] for c in contacts}:
        raise ValueError(f"contact #{contact_id} is not one of {d['company_name']}'s contacts")
    contact = next((c for c in contacts if c["id"] == contact_id), None) or default_recipient(contacts)
    channel = channel or CHANNEL_FOR_CONTACT.get((contact or {}).get("type"), "email")

    oid = db.ensure_outreach(conn, d["candidate_id"], d["company_id"])
    o = need_outreach(conn, oid)
    check_move(o, "sent")
    db.record_event(conn, oid, "sent", at, status="sent", channel=channel, draft_id=draft_id,
                    contact_id=contact["id"] if contact else None)
    return oid, "sent"


def log_reply(conn, outreach_id: int, reply_type: str, note: str | None = None, at: str | None = None) -> None:
    if reply_type not in REPLY_TYPES:
        raise ValueError(f"--type must be one of {REPLY_TYPES}")
    o = need_outreach(conn, outreach_id)
    check_move(o, "replied")
    db.record_event(conn, outreach_id, "replied", parse_when(at), note=note, status="replied", reply_type=reply_type)


def set_status(conn, outreach_id: int, status: str, note: str | None = None, at: str | None = None,
               when: str | None = None) -> None:
    """interview / offer / rejected / ghosted / bounced / withdrawn, or 'answered' (you wrote back).
    when: the interview's date and time (may be in the future)."""
    if status not in MANUAL_STATUSES:
        raise ValueError(f"status must be one of {MANUAL_STATUSES}")
    o = need_outreach(conn, outreach_id)
    if status == "answered":
        if o["status"] not in ("replied", "interview"):
            raise ValueError(f"{o['company_name']} is '{o['status']}': there is no reply to answer")
        db.record_event(conn, outreach_id, "answered", parse_when(at), note=note)
        return
    check_move(o, status)
    scheduled = parse_when(when, allow_future=True) if (status == "interview" and when) else None
    db.record_event(conn, outreach_id, status, parse_when(at), note=note, scheduled_for=scheduled, status=status)
