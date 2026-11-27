"""Part 3 on the website: research + email DRAFTS for a candidate's top matches. Nothing is ever sent.

Pages (OPEN, like the other /match pages: the server only listens on 127.0.0.1. Contacts and
drafts are personal data, so never expose the site to a network):
  /match/candidates/{cid}/drafts                    start a draft run; list of drafted companies
  POST /match/candidates/{cid}/drafts/run           runs `draft` (live log on /match/task)
  /match/candidates/{cid}/drafts/{company}          contacts with sources + both variants; edit + approve
  POST .../{company}/approve                        runs `approve-draft` with your edited subject/body
  POST .../{company}/contacted                      runs `mark-contacted`
  POST .../{company}/sent                           runs `sent` (Part 4, route in outreach_pages.py)
  POST .../{company}/forget                         runs `forget-contact --id`
  /match/candidates/{cid}/drafts/{company}/email.txt   the approved email, to download

Same rules as the rest of the site: pages only READ the database; every write runs the normal CLI
command (long ones through the runner, quick ones with run_now), with every form value checked first.
"""

import re
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .. import config, outreach_config
from ..matching.models import CandidateProfile, Preferences
from ..outreach import contacts as ct
from ..outreach.draft import export_text
from ..outreach.run import SETUPS, drafts_left_today
from ..storage import db
from ..tracker.lifecycle import CHANNELS
from .match_pages import start_task
from . import auth
from .runner import run_now, runner

TOP_CHOICES = [3, 5, 10, 15, 20]
WINDOW = re.compile(r"^[\w ,/();.'-]{2,120}$")
LINK = re.compile(r"^https://[^\s<>\"']{3,200}$")
MAX_SUBJECT, MAX_BODY = 200, 5000

router = APIRouter()


def setup(templates, get_conn, int_or):
    def render(request, template, **ctx):
        return templates.TemplateResponse(request, template, {"active": "match", "busy": runner.busy, **ctx})

    def load(conn, cid: int) -> tuple[CandidateProfile, dict]:
        row = db.get_candidate(conn, cid)
        if row is None:
            raise HTTPException(404, "Candidate not found")
        import json

        return CandidateProfile.model_validate_json(row["profile_json"]), json.loads(row["pii_json"])

    def company_drafts(conn, cid: int, company_id: int) -> list[dict]:
        drafts = [d for d in db.candidate_drafts(conn, cid) if d["company_id"] == company_id]
        if not drafts:
            raise HTTPException(404, "No drafts for this company")
        return drafts

    # ---------- overview + start a run ----------

    @router.get("/match/candidates/{cid}/drafts")
    def drafts_home(request: Request, cid: int, conn=Depends(get_conn)):
        profile, pii = load(conn, cid)
        run_id = db.latest_run_id(conn, cid, reranked_only=True)
        run = db.get_run(conn, run_id) if run_id else None
        prefs = Preferences.model_validate_json(run["prefs_json"]) if run else None
        drafted: dict[int, dict] = {}
        for d in db.candidate_drafts(conn, cid):
            c = drafted.setdefault(d["company_id"], {"company_id": d["company_id"], "name": d["company_name"],
                                                     "checks": [], "approved": None, "created_at": d["created_at"]})
            c["checks"].append((d["variant"], d["check_status"]))
            if d["chosen"]:
                c["approved"] = d["variant"]
        contacted = db.contacted_company_ids(conn, cid)
        top = []
        if run_id:
            for m in db.run_matches(conn, run_id)[:20]:
                state = "contacted" if m["company_id"] in contacted else "drafted" if m["company_id"] in drafted else ""
                top.append({"id": m["company_id"], "name": m["company_name"], "state": state,
                            "role": m["job_title"] if m["job_is_internship"] == 1 else m["pitch_role"]})
        links = {k: any(k in u for u in [*profile.links, *pii.get("links", [])]) for k in ("github", "linkedin")}
        return render(
            request, "drafts_home.html", cid=cid, p=profile, run=run, prefs=prefs, top=top,
            drafted=list(drafted.values()), contacted=contacted, links=links,
            left=drafts_left_today(conn, cid), cap=outreach_config.DAILY_DRAFT_CAP,
            top_choices=TOP_CHOICES, setups=SETUPS, llm_on=bool(config.GEMINI_API_KEY),
            error=request.query_params.get("error"),
        )

    @router.post("/match/candidates/{cid}/drafts/run")
    async def drafts_run(request: Request, cid: int, conn=Depends(get_conn)):
        load(conn, cid)
        if not db.latest_run_id(conn, cid, reranked_only=True):
            raise HTTPException(400, "Run a match with the LLM first (not quick mode).")
        if not config.GEMINI_API_KEY:
            raise HTTPException(400, "Drafting needs a free GEMINI_API_KEY in .env (see README).")
        form = await request.form()
        start_task("Draft emails", [build_draft_args(cid, form, int_or)], auth.user_id(request))
        return RedirectResponse(f"/match/task?next=/match/candidates/{cid}/drafts", status_code=303)

    # ---------- one company: contacts, variants, edit + approve ----------

    @router.get("/match/candidates/{cid}/drafts/{company_id}")
    def draft_company_page(request: Request, cid: int, company_id: int, conn=Depends(get_conn)):
        load(conn, cid)
        drafts = company_drafts(conn, cid, company_id)
        contacts = ct.rank_contacts(db.company_contacts(conn, company_id))
        best = ct.default_recipient(contacts)
        for c in contacts:
            c["level_label"] = contact_label(c)
        return render(
            request, "draft_company.html", cid=cid, company_id=company_id, name=drafts[0]["company_name"],
            drafts=drafts, contacts=contacts, best=best,
            contacted=company_id in db.contacted_company_ids(conn, cid),
            outreach=db.get_outreach(conn, oid) if (oid := db.find_outreach(conn, cid, company_id)) else None,
            channels=CHANNELS, today=date.today(),
            message=request.query_params.get("msg"), lint_out=request.query_params.get("lint"),
        )

    @router.post("/match/candidates/{cid}/drafts/{company_id}/approve")
    async def draft_approve(cid: int, company_id: int, draft_id: int = Form(...), subject: str = Form(...),
                            body: str = Form(...), conn=Depends(get_conn)):
        drafts = company_drafts(conn, cid, company_id)
        if draft_id not in {d["id"] for d in drafts}:
            raise HTTPException(400, "That draft doesn't belong to this company.")
        subject = " ".join(subject.split())
        body = "".join(ch for ch in body.replace("\r\n", "\n") if ch.isprintable() or ch in "\n\t").strip()
        if not (1 <= len(subject) <= MAX_SUBJECT and 1 <= len(body) <= MAX_BODY):
            raise HTTPException(400, f"Subject (1-{MAX_SUBJECT} chars) and body (1-{MAX_BODY} chars) are required.")
        # "--opt=value": text starting with "-" stays text, and exec (no shell) means it can't run anything
        code, out = await run_now(["approve-draft", "--draft", str(draft_id), f"--subject={subject}", f"--body={body}"])
        lint = "; ".join(l[6:] for l in out.splitlines() if l.startswith("lint: "))
        msg = "Approved. Download it below, check the recipient's source link, then send it yourself." if code == 0 \
            else "Could not approve: " + out.strip().splitlines()[-1][:200] if out.strip() else "Could not approve."
        return RedirectResponse(back(cid, company_id, msg=msg, lint=lint), status_code=303)

    @router.post("/match/candidates/{cid}/drafts/{company_id}/contacted")
    async def draft_contacted(cid: int, company_id: int, conn=Depends(get_conn)):
        company_drafts(conn, cid, company_id)
        code, _ = await run_now(["mark-contacted", "--candidate", str(cid), "--company", str(company_id)])
        msg = "Marked as contacted: it will never be drafted again." if code == 0 else "Could not mark it."
        return RedirectResponse(back(cid, company_id, msg=msg), status_code=303)

    @router.post("/match/candidates/{cid}/drafts/{company_id}/forget")
    async def draft_forget(request: Request, cid: int, company_id: int, contact_id: int = Form(...),
                           conn=Depends(get_conn)):
        company_drafts(conn, cid, company_id)
        if not auth.is_admin(request):  # contacts are shared by every student: only the admin removes them
            raise HTTPException(403, "Only the admin can forget a contact (it's removed for everyone).")
        if contact_id not in {c["id"] for c in db.company_contacts(conn, company_id)}:
            raise HTTPException(400, "Unknown contact")
        code, _ = await run_now(["forget-contact", "--company", str(company_id), "--id", str(contact_id)])
        msg = "Contact forgotten: it won't be saved again." if code == 0 else "Could not forget it."
        return RedirectResponse(back(cid, company_id, msg=msg), status_code=303)

    @router.get("/match/candidates/{cid}/drafts/{company_id}/email.txt")
    def draft_download(cid: int, company_id: int, conn=Depends(get_conn)):
        drafts = company_drafts(conn, cid, company_id)
        chosen = next((d for d in drafts if d["chosen"]), None)
        if chosen is None:
            raise HTTPException(404, "Approve a variant first.")
        best = ct.default_recipient(db.company_contacts(conn, company_id))
        text = export_text(chosen["company_name"], best, chosen["subject"], chosen["edited_body"] or chosen["body"])
        slug = re.sub(r"[^a-z0-9]+", "-", chosen["company_name"].lower()).strip("-") or "company"
        return Response(text, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{slug}.txt"'})

    return router


def back(cid: int, company_id: int, **params) -> str:
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in params.items() if v})
    return f"/match/candidates/{cid}/drafts/{company_id}" + (f"?{query}" if query else "")


def contact_label(c: dict) -> str:
    """Short, plain-words version of the ranking level for the contacts table."""
    if c.get("is_guess"):
        return "unverified guess"
    level = ct.level(c)
    return {1: "company email", 2: "verified email", 3: "listed email", 4: "DM", 4.4: "unplaced inbox",
            4.5: "wrong inbox", 4.6: "no mail server", 5: "apply page"}.get(level, "profile / info")


def build_draft_args(cid: int, form, int_or) -> list[str]:
    """Form -> `draft` CLI arguments. Every value is checked against a fixed list or a strict pattern."""
    top = int_or(form.get("top"), 5, 1, 50)
    if top not in TOP_CHOICES:
        top = 5
    args = ["draft", "--candidate", str(cid), "--top", str(top)]
    window = " ".join((form.get("window") or "").split())
    if not WINDOW.match(window):
        raise HTTPException(400, "Say when you can intern, e.g. 'Summer 2027' (letters, numbers and , / ( ) ; . - only).")
    args.append(f"--window={window}")
    setup = form.get("setup") or "either"
    if setup not in SETUPS:
        raise HTTPException(400, "Unknown work setup")
    args.append(f"--setup={setup}")
    for key in ("github", "linkedin", "portfolio"):
        url = (form.get(key) or "").strip()
        if url:
            if not LINK.match(url):
                raise HTTPException(400, f"The {key} link must start with https:// and have no spaces.")
            args.append(f"--{key}={url}")
    if form.get("redo"):
        args.append("--redo")
    return args
