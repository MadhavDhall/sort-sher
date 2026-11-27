"""Step D: what actually works. The core metric of Sort Sherr is REPLY RATE.

    reply rate     = outreach that got a reply / outreach sent and NOT bounced (a bounce never reached anyone)
    positive rate  = positive replies / the same
    interview rate = reached an interview / the same
    time to reply  = median days from the first email to the first reply

Every breakdown shows its sample size n. Groups with n < SMALL_N are flagged "too few to judge", and we
only name a leading group when EVERY group compared has n >= SMALL_N: with 4 emails, 2 replies vs 1
reply is luck, not a finding.
"""

import csv
import io
import re
from datetime import datetime
from statistics import median

from .. import tracker_config as cfg
from ..storage import db
from .followups import followups_sent, local_date

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FOUNDER = re.compile(r"founder|\bceo\b|\bcto\b|chief", re.I)
HIRING = re.compile(r"head of eng|engineering (lead|manager)|vp eng|recruit|talent|hiring|people", re.I)
HIRING_INBOXES = {"jobs", "careers", "hiring", "talent", "recruiting", "internships", "interns", "people"}
GENERAL_INBOXES = {"hello", "contact", "info", "team", "hi"}


# ---------- one flat record per outreach ----------


def recipient_role(o: dict) -> str:
    """founder / hiring or eng lead / hiring inbox / general inbox / unknown"""
    role = o.get("contact_role") or ""
    if FOUNDER.search(role):
        return "founder"
    if HIRING.search(role):
        return "hiring / eng lead"
    value = (o.get("contact_value") or "").lower()
    if o.get("contact_type") == "email" and "@" in value:
        local = value.split("@")[0]
        if "founder" in local:
            return "founder"  # founders@ goes to the founders
        if local in HIRING_INBOXES:
            return "hiring inbox"
        if local in GENERAL_INBOXES:
            return "general inbox"
    return "unknown"


def bucket(value, buckets, unknown="unknown") -> str:
    """Score buckets are (lower bound, label), team buckets are (upper bound, label)."""
    if value is None:
        return unknown
    if buckets is cfg.SCORE_BUCKETS:
        return next(label for low, label in buckets if value >= low)
    return next(label for high, label in buckets if value <= high)


def batch_year(yc_batch: str | None) -> str:
    year = re.search(r"(20\d\d)", yc_batch or "")
    return f"YC {year.group(1)}" if year else ("YC" if yc_batch else "non-YC")


def record(o: dict, events: list[dict]) -> dict:
    """Everything metrics need about one outreach, in plain fields (also the CSV export row)."""
    sent = next((e for e in events if e["kind"] == "sent"), None)
    reply = next((e for e in events if e["kind"] == "replied"), None)
    kinds = {e["kind"] for e in events}
    followups = followups_sent(events)
    days_to_reply = None
    reply_after = None
    if sent and reply:
        days_to_reply = (datetime.fromisoformat(reply["at"]) - datetime.fromisoformat(sent["at"])).total_seconds() / 86400
        reply_after = sum(1 for f in followups if f["at"] <= reply["at"])  # 0 = the first email got it
    return {
        "outreach_id": o["id"], "company": o["company_name"], "status": o["status"], "channel": o["channel"],
        "sent_on": local_date(sent["at"]).isoformat() if sent else "",
        "weekday_sent": WEEKDAYS[local_date(sent["at"]).weekday()] if sent else "",
        "is_sent": bool(sent), "bounced": "bounced" in kinds,
        "replied": bool(reply), "reply_type": o["reply_type"] or "",
        "interview": bool(kinds & {"interview", "offer"}), "offer": "offer" in kinds,
        "followups_sent": len(followups), "reply_after_followups": reply_after,
        "days_to_reply": round(days_to_reply, 1) if days_to_reply is not None else None,
        "variant": o["draft_style"] or "logged by hand",
        "contact_type": o["contact_type"] or "unknown",
        "contact_source": o["contact_source"] or "unknown",
        "recipient_role": recipient_role(o),
        "match_score": o["match_score"],
        "score_bucket": bucket(o["match_score"], cfg.SCORE_BUCKETS),
        "yc_batch": batch_year(o["yc_batch"]),
        "team_size": bucket(o["team_size"], cfg.TEAM_BUCKETS),
    }


def records(conn, candidate_id: int) -> list[dict]:
    events = db.candidate_events(conn, candidate_id)
    return [record(o, events.get(o["id"], [])) for o in db.candidate_outreach(conn, candidate_id)]


# ---------- rates ----------


def rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def group(rows: list[dict], key: str) -> list[dict]:
    """Delivered outreach grouped by one field: n, replies, rates, and a small-n flag."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r[key]), []).append(r)
    out = []
    for name, rs in groups.items():
        n, replies = len(rs), sum(r["replied"] for r in rs)
        out.append({"group": name, "n": n, "replies": replies, "reply_rate": rate(replies, n),
                    "positive_rate": rate(sum(r["reply_type"] == "positive" for r in rs), n),
                    "small": n < cfg.SMALL_N})
    out.sort(key=lambda g: (-g["n"], g["group"]))
    return out


def verdict(groups: list[dict]) -> str:
    """One honest sentence about a breakdown."""
    if len(groups) < 2:
        return "Only one group so far: nothing to compare."
    if any(g["small"] for g in groups):
        return f"Too few to judge: every group needs at least {cfg.SMALL_N} sent emails before comparing."
    best = max(groups, key=lambda g: g["reply_rate"])
    return f"\"{best['group']}\" leads with {best['reply_rate']:.0%} (n={best['n']}). Still check it holds as n grows."


def by_message(rows: list[dict]) -> list[dict]:
    """Which message got the reply: the first email, follow-up 1 or follow-up 2.
    n = outreach where that message was sent; replies = replies that came after it (before the next one)."""
    out = []
    for k, name in enumerate(["first email", "follow-up 1", "follow-up 2"][: cfg.MAX_FOLLOWUPS + 1]):
        sent = [r for r in rows if r["followups_sent"] >= k]
        replies = sum(1 for r in sent if r["reply_after_followups"] == k)
        out.append({"group": name, "n": len(sent), "replies": replies, "reply_rate": rate(replies, len(sent)),
                    "positive_rate": None, "small": len(sent) < cfg.SMALL_N})
    return out


BREAKDOWNS = [
    ("variant", "Email variant"),
    ("recipient_role", "Recipient role"),
    ("contact_type", "Contact type"),
    ("contact_source", "Where the contact came from"),
    ("score_bucket", "Match score"),
    ("yc_batch", "Company batch"),
    ("team_size", "Team size"),
    ("weekday_sent", "Day of week sent"),
    ("channel", "Channel"),
]


def summary(conn, candidate_id: int) -> dict:
    rows = records(conn, candidate_id)
    sent = [r for r in rows if r["is_sent"]]
    delivered = [r for r in sent if not r["bounced"]]
    replied = [r for r in delivered if r["replied"]]
    days = [r["days_to_reply"] for r in replied if r["days_to_reply"] is not None]
    n = len(delivered)
    breakdowns = [{"key": key, "title": title, "groups": group(delivered, key)} for key, title in BREAKDOWNS]
    breakdowns.insert(2, {"key": "message", "title": "Which message got the reply", "groups": by_message(delivered)})
    for b in breakdowns:
        b["verdict"] = verdict(b["groups"])
    return {
        "rows": rows,
        "funnel": [
            ("drafted", len(rows)),
            ("sent", len(sent)),
            ("replied", len(replied)),
            ("interview", sum(r["interview"] for r in delivered)),
            ("offer", sum(r["offer"] for r in delivered)),
        ],
        "sent": len(sent), "bounced": len(sent) - n, "delivered": n,
        "reply_rate": rate(len(replied), n),
        "positive_rate": rate(sum(r["reply_type"] == "positive" for r in replied), n),
        "interview_rate": rate(sum(r["interview"] for r in delivered), n),
        "median_days_to_reply": round(median(days), 1) if days else None,
        "small": n < cfg.SMALL_N,
        "breakdowns": breakdowns,
    }


# ---------- CSV export ----------

CSV_COLUMNS = ["outreach_id", "company", "status", "channel", "sent_on", "weekday_sent", "bounced", "replied",
               "reply_type", "interview", "offer", "followups_sent", "reply_after_followups", "days_to_reply",
               "variant", "contact_type", "contact_source", "recipient_role", "match_score", "score_bucket",
               "yc_batch", "team_size"]


def to_csv(rows: list[dict]) -> str:
    """One row per outreach. No contact addresses or notes: only what analysis needs."""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


# ---------- Step E: a light feedback signal for `match` (shown, never used to re-rank) ----------

FEEDBACK_KEYS = [("team_size", "team size"), ("yc_batch", "batch"), ("recipient_role", "recipient")]


def feedback(conn, candidate_id: int) -> dict | None:
    """Which kinds of companies replied so far. None when nothing has been sent yet."""
    delivered = [r for r in records(conn, candidate_id) if r["is_sent"] and not r["bounced"]]
    if not delivered:
        return None
    return {"delivered": len(delivered), "replies": sum(r["replied"] for r in delivered),
            "by": [(title, group(delivered, key)) for key, title in FEEDBACK_KEYS]}
