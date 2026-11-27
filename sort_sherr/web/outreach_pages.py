"""Part 4 on the website: the outreach tracker, inside Match me. Nothing is ever sent from here.

Pages (OPEN, like the other /match pages: the server only listens on 127.0.0.1):
  /match/candidates/{cid}/outreach                 Today (what's due) + every outreach
  /match/candidates/{cid}/outreach/stats           reply rate, funnel, breakdowns (every group shows n)
  /match/candidates/{cid}/outreach/report.html     the self-contained HTML report (download)
  /match/candidates/{cid}/outreach/export.csv      one row per outreach (download)
  /match/candidates/{cid}/outreach/{oid}           timeline + buttons: log reply, set status, follow-ups
  POST .../{oid}/reply | status | sent | approve   run `reply` / `status` / `sent` / `approve-draft`
  POST .../{oid}/followup                          runs `followup --no-review` (live log on /match/task)
  POST /match/candidates/{cid}/drafts/{company}/sent   "I sent it": runs `sent <approved draft>`

Same rules as the rest of the site: pages only READ the database; every write runs the normal CLI command
(quick ones with run_now, the LLM one through the runner), and every form value is checked first.
"""

import re
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import config, tracker_config
from ..storage import db
from ..tracker.followups import local_date, next_action, due_items
from ..tracker.lifecycle import CHANNELS, MANUAL_STATUSES, REPLY_TYPES, allowed_statuses
from ..tracker.metrics import pct, records, summary, to_csv
from ..tracker.report import render_report
from .match_pages import start_task
from .runner import run_now, runner

# A date from <input type="date"> or a date+time from <input type="datetime-local">
WHEN = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?$")
MAX_NOTE, MAX_VALUE, MAX_SUBJECT, MAX_BODY = 300, 300, 200, 5000

router = APIRouter()


def clean_text(text: str, limit: int) -> str:
    """One line of printable text, at most `limit` characters."""
    text = "".join(ch for ch in " ".join((text or "").split()) if ch.isprintable())  # newlines -> spaces first
    if len(text) > limit:
        raise HTTPException(400, f"Keep it under {limit} characters.")
    return text


def when_arg(value: str, flag: str) -> list[str]:
    """Form date -> ['--at=2026-11-20 14:30'] (or [] when empty)."""
    value = (value or "").strip()
    if not value:
        return []
    if not WHEN.match(value):
        raise HTTPException(400, "Dates look like 2026-11-20 (or 2026-11-20 14:30).")
    return [f"--{flag}={value.replace('T', ' ')}"]


def setup(templates, get_conn, int_or):
    from datetime import datetime

    # Event times are stored in UTC; show them on YOUR clock
    templates.env.filters["localday"] = lambda v: local_date(v) if v else ""
    templates.env.filters["localtime"] = lambda v: datetime.fromisoformat(v).astimezone().strftime("%Y-%m-%d %H:%M") if v else ""

    def render(request, template, **ctx):
        return templates.TemplateResponse(request, template, {"active": "match", "busy": runner.busy, "pct": pct, **ctx})

    def candidate(conn, cid: int) -> dict:
        row = db.get_candidate(conn, cid)
        if row is None:
            raise HTTPException(404, "Candidate not found")
        return row

    def outreach_of(conn, cid: int, oid: int) -> dict:
        """The outreach, only if it belongs to THIS candidate (ids in URLs are never trusted)."""
        o = db.get_outreach(conn, oid)
        if o is None or o["candidate_id"] != cid:
            raise HTTPException(404, "Outreach not found")
        return o

    def back(cid: int, oid: int | None, code: int, out: str, ok: str) -> RedirectResponse:
        msg = ok if code == 0 else "Could not do that: " + (out.strip().splitlines() or ["unknown error"])[-1][:200]
        from urllib.parse import urlencode

        page = f"/match/candidates/{cid}/outreach" + (f"/{oid}" if oid else "")
        return RedirectResponse(f"{page}?{urlencode({'msg': msg})}", status_code=303)

    # ---------- overview: Today + all outreach ----------

    @router.get("/match/candidates/{cid}/outreach")
    def outreach_home(request: Request, cid: int, conn=Depends(get_conn)):
        candidate(conn, cid)
        today = date.today()
        events = db.candidate_events(conn, cid)
        rows = []
        for o in db.candidate_outreach(conn, cid):
            ev = events.get(o["id"], [])
            sent = next((e for e in ev if e["kind"] == "sent"), None)
            rows.append({**o, "sent_on": local_date(sent["at"]) if sent else None,
                         "next": next_action(o, ev, today)})
        s = summary(conn, cid)
        return render(request, "outreach_home.html", cid=cid, today=today, due=due_items(conn, cid, today),
                      rows=rows, s=s, funnel=dict(s["funnel"]), message=request.query_params.get("msg"))

    # ---------- stats + downloads (registered before /{oid}, so "stats" isn't read as an id) ----------

    @router.get("/match/candidates/{cid}/outreach/stats")
    def outreach_stats(request: Request, cid: int, conn=Depends(get_conn)):
        candidate(conn, cid)
        s = summary(conn, cid)
        return render(request, "outreach_stats.html", cid=cid, s=s, funnel=dict(s["funnel"]), small_n=tracker_config.SMALL_N)

    @router.get("/match/candidates/{cid}/outreach/report.html")
    def outreach_report(cid: int, conn=Depends(get_conn)):
        candidate(conn, cid)
        return HTMLResponse(render_report(conn, cid),
                            headers={"Content-Disposition": f'attachment; filename="sort-sherr-outreach-{cid}.html"'})

    @router.get("/match/candidates/{cid}/outreach/export.csv")
    def outreach_csv(cid: int, conn=Depends(get_conn)):
        candidate(conn, cid)
        return Response(to_csv(records(conn, cid)), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="sort-sherr-outreach-{cid}.csv"'})

    # ---------- one outreach ----------

    @router.get("/match/candidates/{cid}/outreach/{oid}")
    def outreach_one(request: Request, cid: int, oid: int, conn=Depends(get_conn)):
        o = outreach_of(conn, cid, oid)
        events = db.outreach_events(conn, oid)
        first = db.get_draft(conn, o["draft_id"]) if o["draft_id"] else None
        logged = {e["followup_number"] for e in events if e["kind"] == "followup_sent"}
        return render(
            request, "outreach_one.html", cid=cid, o=o, events=events, first=first,
            followups=db.followup_drafts(conn, oid), logged=logged,
            action=next_action(o, events, date.today()), today=date.today(),
            channels=CHANNELS, reply_types=REPLY_TYPES, statuses=allowed_statuses(o["status"]),
            llm_on=bool(config.GEMINI_API_KEY), max_followups=tracker_config.MAX_FOLLOWUPS,
            message=request.query_params.get("msg"),
        )

    @router.post("/match/candidates/{cid}/outreach/{oid}/reply")
    async def post_reply(cid: int, oid: int, type: str = Form(...), note: str = Form(""), at: str = Form(""),
                         conn=Depends(get_conn)):
        outreach_of(conn, cid, oid)
        if type not in REPLY_TYPES:
            raise HTTPException(400, "Unknown reply type")
        args = ["reply", str(oid), "--type", type, *when_arg(at, "at")]
        if note.strip():
            args.append(f"--note={clean_text(note, MAX_NOTE)}")
        code, out = await run_now(args)
        return back(cid, oid, code, out, f"Logged a {type} reply. Follow-ups stop here.")

    @router.post("/match/candidates/{cid}/outreach/{oid}/status")
    async def post_status(cid: int, oid: int, status: str = Form(...), note: str = Form(""), at: str = Form(""),
                          when: str = Form(""), conn=Depends(get_conn)):
        outreach_of(conn, cid, oid)
        if status not in MANUAL_STATUSES:
            raise HTTPException(400, "Unknown status")
        args = ["status", str(oid), status, *when_arg(at, "at"), *when_arg(when, "when")]
        if note.strip():
            args.append(f"--note={clean_text(note, MAX_NOTE)}")
        code, out = await run_now(args)
        return back(cid, oid, code, out, f"Saved: {status}.")

    @router.post("/match/candidates/{cid}/outreach/{oid}/sent")
    async def post_followup_sent(cid: int, oid: int, draft_id: int = Form(...), at: str = Form(""),
                                 conn=Depends(get_conn)):
        outreach_of(conn, cid, oid)
        if draft_id not in {d["id"] for d in db.followup_drafts(conn, oid)}:
            raise HTTPException(400, "That draft isn't a follow-up of this outreach.")
        code, out = await run_now(["sent", str(draft_id), *when_arg(at, "at")])
        return back(cid, oid, code, out, "Logged the follow-up as sent.")

    @router.post("/match/candidates/{cid}/outreach/{oid}/approve")
    async def post_approve(cid: int, oid: int, draft_id: int = Form(...), subject: str = Form(...),
                           body: str = Form(...), conn=Depends(get_conn)):
        outreach_of(conn, cid, oid)
        if draft_id not in {d["id"] for d in db.followup_drafts(conn, oid)}:
            raise HTTPException(400, "That draft isn't a follow-up of this outreach.")
        subject = clean_text(subject, MAX_SUBJECT)
        body = "".join(ch for ch in body.replace("\r\n", "\n") if ch.isprintable() or ch in "\n\t").strip()
        if not subject or not 1 <= len(body) <= MAX_BODY:
            raise HTTPException(400, f"Subject and body (1-{MAX_BODY} chars) are required.")
        code, out = await run_now(["approve-draft", "--draft", str(draft_id), f"--subject={subject}", f"--body={body}"])
        lint = "; ".join(l[6:] for l in out.splitlines() if l.startswith("lint: "))
        return back(cid, oid, code, out, "Approved. Send it in the same thread yourself, then mark it sent."
                    + (f" Lint: {lint}" if lint else ""))

    @router.post("/match/candidates/{cid}/outreach/{oid}/followup")
    async def post_followup(cid: int, oid: int, value: str = Form(""), redo: str = Form(""), conn=Depends(get_conn)):
        o = outreach_of(conn, cid, oid)
        if o["status"] != "sent":
            raise HTTPException(400, "Follow-ups are only for emails still waiting for a reply.")
        if not config.GEMINI_API_KEY:
            raise HTTPException(400, "Follow-up drafts need a free GEMINI_API_KEY in .env (see README).")
        args = ["followup", str(oid), "--no-review"]
        if value.strip():
            args.append(f"--value={clean_text(value, MAX_VALUE)}")
        if redo:
            args.append("--redo")
        start_task("Draft follow-up", [args])
        return RedirectResponse(f"/match/task?next=/match/candidates/{cid}/outreach/{oid}", status_code=303)

    # ---------- from the drafts page: "I sent it" ----------

    @router.post("/match/candidates/{cid}/drafts/{company_id}/sent")
    async def draft_sent(cid: int, company_id: int, channel: str = Form("email"), at: str = Form(""),
                         conn=Depends(get_conn)):
        chosen = [d for d in db.candidate_drafts(conn, cid) if d["company_id"] == company_id and d["chosen"]]
        if not chosen:
            raise HTTPException(400, "Approve a variant first, then send it yourself.")
        if channel not in CHANNELS:
            raise HTTPException(400, "Unknown channel")
        code, out = await run_now(["sent", str(chosen[0]["id"]), "--channel", channel, *when_arg(at, "at")])
        oid = db.find_outreach(conn, cid, company_id)
        return back(cid, oid if code == 0 else None, code, out,
                    "Logged as sent. The follow-up clock has started: see Today on the Outreach page.")

    return router
