"""Part 4: follow-up dates (business days, counted from the ACTUAL previous send), stopping on a reply or
bounce, ghosting after the last follow-up, and the priority order of `due`.

Timestamps are 12:00 UTC so the local calendar day is the same in every time zone from UTC-11 to UTC+11."""

from datetime import date

import pytest

from sort_sherr import tracker_config as cfg
from sort_sherr.storage import db
from sort_sherr.tracker import lifecycle
from sort_sherr.tracker.followups import add_business_days, due_items, next_action


def at(day: str) -> str:
    return f"{day}T12:00:00+00:00"


def ev(kind, day, n=None, scheduled=None):
    return {"kind": kind, "at": at(day), "followup_number": n, "scheduled_for": scheduled}


SENT_FRI = [ev("drafted", "2026-11-19"), ev("sent", "2026-11-20")]  # 20 Nov 2026 is a Friday


def action(status, events, today, reply_type=None):
    return next_action({"status": status, "reply_type": reply_type}, events, date.fromisoformat(today))


def test_business_days():
    fri, sat = date(2026, 11, 20), date(2026, 11, 21)
    assert fri.weekday() == 4 and sat.weekday() == 5
    assert add_business_days(fri, 1) == date(2026, 11, 23)  # Friday + 1 = Monday
    assert add_business_days(fri, 5) == date(2026, 11, 27)  # next Friday
    assert add_business_days(sat, 1) == date(2026, 11, 23)  # weekend days don't count
    assert add_business_days(sat, 5) == date(2026, 11, 27)
    assert add_business_days(date(2026, 11, 23), 7) == date(2026, 12, 2)  # Mon + 7 = Wed of the next week


def test_first_followup_due_after_5_business_days():
    assert cfg.FIRST_FOLLOWUP_DAYS == 5
    a = action("sent", SENT_FRI, "2026-11-26")  # Thursday: not yet
    assert a["kind"] == "followup" and a["number"] == 1 and a["due_on"] == date(2026, 11, 27)
    assert a["due_on"] > date(2026, 11, 26)
    assert action("sent", SENT_FRI, "2026-11-27")["due_on"] == date(2026, 11, 27)  # due on Friday


def test_second_followup_counts_from_the_actual_first_followup():
    # follow-up 1 was due Fri 27 Nov but sent late, on Tue 1 Dec -> follow-up 2 due 7 business days later
    events = [*SENT_FRI, ev("followup_sent", "2026-12-01", n=1)]
    a = action("sent", events, "2026-12-02")
    assert (a["number"], a["due_on"]) == (2, date(2026, 12, 10))
    assert "last one" in a["label"]


def test_ghosted_after_the_last_followup():
    events = [*SENT_FRI, ev("followup_sent", "2026-11-27", n=1), ev("followup_sent", "2026-12-08", n=2)]
    a = action("sent", events, "2026-12-17")
    assert a["kind"] == "ghost" and a["due_on"] == date(2026, 12, 17)  # Tue 8 Dec + 7 business days


@pytest.mark.parametrize("status", ["bounced", "ghosted", "rejected", "offer", "withdrawn"])
def test_final_statuses_need_nothing(status):
    assert action(status, [*SENT_FRI, ev(status, "2026-11-23")], "2026-12-30") is None


def test_reply_stops_followups_and_asks_you_to_answer():
    events = [*SENT_FRI, ev("replied", "2026-11-24")]
    a = action("replied", events, "2026-12-30", reply_type="positive")
    assert a["kind"] == "reply" and a["priority"] == 2  # not a follow-up, ever
    assert action("replied", [*events, ev("answered", "2026-11-24")], "2026-12-30") is None
    neg = action("replied", events, "2026-12-30", reply_type="negative")
    assert "rejected" in neg["label"] and neg["priority"] == 3


def test_reply_between_followups_stops_the_second_one(outreach_db):
    conn, cand = outreach_db["conn"], outreach_db["cand"]
    oid, _ = lifecycle.log_sent(conn, outreach_db["drafts"][0], at="2026-11-02")
    db.record_event(conn, oid, "followup_sent", at("2026-11-09"), followup_number=1)
    lifecycle.log_reply(conn, oid, "neutral", at="2026-11-10")
    items = due_items(conn, cand, today=date(2026, 12, 30))
    assert [i["action"]["kind"] for i in items] == ["reply"]


def test_interview_soon_shows_up_far_away_doesnt():
    events = [*SENT_FRI, ev("replied", "2026-11-23"), ev("interview", "2026-11-24", scheduled=at("2026-12-10"))]
    assert action("interview", events, "2026-12-01")["due_on"] > date(2026, 12, 1)  # 9 days away: not due
    soon = action("interview", events, "2026-12-08")
    assert soon["due_on"] <= date(2026, 12, 8) and soon["priority"] == 1
    assert "log the result" in action("interview", events, "2026-12-11")["label"]


def test_due_marks_ghosted_and_sorts_by_priority(outreach_db):
    from sort_sherr.models import Company

    conn, cand = outreach_db["conn"], outreach_db["cand"]
    # Acme: two follow-ups, then silence -> ghosted today
    acme, _ = lifecycle.log_sent(conn, outreach_db["drafts"][0], at="2026-10-01")
    db.record_event(conn, acme, "followup_sent", at("2026-10-08"), followup_number=1)
    db.record_event(conn, acme, "followup_sent", at("2026-10-19"), followup_number=2)
    # Beta: positive reply waiting for an answer
    beta_co = db.upsert_company(conn, Company(name="Beta", domain="beta.io"))
    beta = db.ensure_outreach(conn, cand, beta_co)
    db.record_event(conn, beta, "sent", at("2026-11-02"), status="sent")
    lifecycle.log_reply(conn, beta, "positive", at="2026-11-05")
    # Gamma: first follow-up overdue
    gamma_co = db.upsert_company(conn, Company(name="Gamma", domain="gamma.io"))
    gamma = db.ensure_outreach(conn, cand, gamma_co)
    db.record_event(conn, gamma, "sent", at("2026-11-02"), status="sent")

    items = due_items(conn, cand, today=date(2026, 11, 20), apply_ghost=True)
    assert [(i["company_name"], i["action"]["kind"]) for i in items] == \
        [("Beta", "reply"), ("Gamma", "followup"), ("Acme Labs", "ghost")]
    assert items[1]["days_late"] == 11  # due Mon 9 Nov
    assert db.get_outreach(conn, acme)["status"] == "ghosted"
    assert db.outreach_events(conn, acme)[-1]["source"] == "auto"
    assert [i["company_name"] for i in due_items(conn, cand, today=date(2026, 11, 20))] == ["Beta", "Gamma"]


def test_followup_draft_is_linked_in_due(outreach_db):
    conn, cand = outreach_db["conn"], outreach_db["cand"]
    oid, _ = lifecycle.log_sent(conn, outreach_db["drafts"][0], at="2026-11-02")
    d = db.save_draft(conn, {"match_id": None, "candidate_id": cand, "company_id": outreach_db["company"],
                             "variant": "A", "style": "follow-up", "subject": "Re: x", "body": "Hi", "facts_used": [],
                             "student_items_used": [], "check_status": "ok", "check_notes": [], "lint_flags": [],
                             "outreach_id": oid, "followup_number": 1})
    [item] = due_items(conn, cand, today=date(2026, 11, 20))
    assert item["action"]["draft_id"] == d
    lifecycle.log_sent(conn, d, at="2026-11-19")  # sending the follow-up draft logs followup_sent
    assert db.outreach_events(conn, oid)[-1]["followup_number"] == 1
    assert due_items(conn, cand, today=date(2026, 11, 20)) == []  # next one isn't due for 7 business days
