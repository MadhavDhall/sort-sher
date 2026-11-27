"""Part 2 on the website: upload a resume, pick preferences, see the ranked shortlist.

Pages (locally OPEN, because the server only listens on 127.0.0.1. HOSTED: every page needs a login
and auth.owner_guard (added in app.py) lets a student see only their own resumes, runs and tasks):
  /match                              upload a resume; list of parsed resumes
  POST /match/upload                  save the file, guess the name -> confirm page
  POST /match/parse                   runs `parse-resume` (with the confirmed name + grad year)
  /match/candidates/{id}              profile, suggested roles, the match form, past runs
  POST /match/candidates/{id}/run     runs `match`
  /match/runs/{id}                    the ranked shortlist
  /match/runs/{id}/shortlist.csv      the same, as a spreadsheet
  /match/task                         live log of the running task, then jumps to the result
  /match/task.json, POST /match/stop  status / stop, but ONLY for Match tasks (never an admin crawl)

Same rules as the admin page: pages only READ the database; anything that writes runs the normal
CLI command through the runner, with every form value checked first.
"""

import csv
import io
import json
import math
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from .. import config, match_config
from ..matching.models import ROLE_CHOICES, CandidateProfile, Preferences
from ..matching.roles import suggest_roles
from ..storage import db
from . import auth
from .runner import PROJECT_ROOT, runner

UPLOAD_DIR = PROJECT_ROOT / "uploads"  # in .gitignore
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
FILE_ID = re.compile(r"^[0-9a-f]{16}\.(pdf|docx)$")  # files are saved under a hash, never the user's file name
NAME = re.compile(r"^[^\W\d_][^\W\d_ .'-]*(?:[ .'-]+[^\W\d_]+)*\.?$")  # letters, spaces, . ' -
LOCATION = re.compile(r"^[\w .,'-]{2,40}$")
WINDOW = re.compile(r"^[\w ,/-]{0,30}$")
SECONDS_PER_LLM_CALL = 8  # rough: 6 s free-tier spacing + answer time

router = APIRouter()


MATCH_TASKS = ("Parse resume", "Match", "Draft emails", "Draft follow-up")  # the only runner tasks these open pages may see or stop


def setup(templates, get_conn, int_or):
    """Called once from app.py, so this module can share its templates, DB and helpers."""

    def render(request, name, **ctx):
        return templates.TemplateResponse(request, name, {"active": "match", "busy": runner.busy, **ctx})

    # ---------- upload ----------

    def own_candidates(request, conn) -> list[dict]:
        """This student's resumes (all of them for the admin, or locally)."""
        return db.list_candidates(conn, None if auth.is_admin(request) else auth.user_id(request))

    @router.get("/match")
    def match_home(request: Request, conn=Depends(get_conn)):
        candidates = []
        for c in own_candidates(request, conn):
            p = CandidateProfile.model_validate_json(c["profile_json"])
            candidates.append({**c, "p": p})
        return render(request, "match_home.html", candidates=candidates, llm_on=bool(config.GEMINI_API_KEY))

    @router.post("/match/upload")
    async def match_upload(request: Request, resume: UploadFile = File(...), conn=Depends(get_conn)):
        from ..matching.resume import guess_name

        suffix = (resume.filename or "").lower().rsplit(".", 1)[-1]
        data = await resume.read(MAX_UPLOAD_BYTES + 1)
        error = None
        if suffix not in ("pdf", "docx"):
            error = "Please upload a .pdf or .docx file."
        elif len(data) > MAX_UPLOAD_BYTES:
            error = "That file is over 5 MB. Export a smaller PDF."
        elif not data.startswith(b"%PDF" if suffix == "pdf" else b"PK"):  # check the content, not just the name
            error = f"That doesn't look like a real .{suffix} file."
        from ..matching.resume import resume_key

        digest = resume_key(data, auth.user_id(request))
        existing = None if error else db.get_candidate_by_hash(conn, digest)
        if existing:  # same file parsed before: no need to send anything again
            return RedirectResponse(f"/match/candidates/{existing['id']}", status_code=303)
        if not error and not auth.is_admin(request) and \
                len(own_candidates(request, conn)) >= config.MAX_RESUMES_PER_USER:
            error = f"You already have {config.MAX_RESUMES_PER_USER} resumes here. Delete one first (on its page)."
        if error:
            return render(request, "match_home.html", candidates=[], error=error, llm_on=bool(config.GEMINI_API_KEY))

        file_id = f"{digest[:16]}.{suffix}"
        UPLOAD_DIR.mkdir(exist_ok=True)
        (UPLOAD_DIR / file_id).write_bytes(data)
        try:
            guess = guess_name(UPLOAD_DIR / file_id)  # local only
        except Exception:
            guess = None
        return render(request, "match_confirm.html", file_id=file_id, guess=guess or "",
                      llm_on=bool(config.GEMINI_API_KEY))

    @router.post("/match/parse")
    async def match_parse(request: Request, file_id: str = Form(...), name: str = Form(...),
                          grad_year: str = Form(...)):
        name = " ".join(name.split())
        year = int_or(grad_year, None, 2000, 2040)
        if not FILE_ID.match(file_id) or not (UPLOAD_DIR / file_id).is_file():
            raise HTTPException(400, "Unknown upload. Please upload the file again.")
        if not (3 <= len(name) <= 80 and NAME.match(name)):
            raise HTTPException(400, "Please enter your name as letters and spaces, as it appears on your resume.")
        if year is None or str(year) != grad_year.strip():
            raise HTTPException(400, "Graduation year should look like 2028.")
        if not config.GEMINI_API_KEY:
            raise HTTPException(400, "Parsing needs a free GEMINI_API_KEY in .env (see README).")
        args = ["parse-resume", f"uploads/{file_id}", f"--name={name}", "--grad-year", str(year), "--delete-file"]
        if auth.user_id(request):
            args += ["--user", str(auth.user_id(request))]
        start_task("Parse resume", [args], auth.user_id(request))
        return RedirectResponse(f"/match/task?next=/match/resume/{file_id}", status_code=303)

    @router.get("/match/resume/{file_id}")
    def match_resume_done(request: Request, file_id: str, conn=Depends(get_conn)):
        """Where the parse task jumps when it's done: find the candidate made from this file (the file
        itself is already deleted; its name is the start of the resume's hash)."""
        if not FILE_ID.match(file_id):
            raise HTTPException(404, "Unknown upload")
        row = db.get_candidate_by_hash_prefix(conn, file_id[:16], auth.user_id(request))
        if row is None:
            raise HTTPException(404, "This resume isn't parsed yet. Check the task log.")
        return RedirectResponse(f"/match/candidates/{row['id']}", status_code=303)

    # ---------- candidate + match form ----------

    def load_candidate(conn, candidate_id: int) -> CandidateProfile:
        row = db.get_candidate(conn, candidate_id)
        if row is None:
            raise HTTPException(404, "Candidate not found")
        return CandidateProfile.model_validate_json(row["profile_json"])

    @router.get("/match/candidates/{candidate_id}")
    def candidate_page(request: Request, candidate_id: int, conn=Depends(get_conn)):
        profile = load_candidate(conn, candidate_id)
        runs = db.candidate_runs(conn, candidate_id)
        for r in runs:
            r["prefs"] = Preferences.model_validate_json(r["prefs_json"])
        shortlist_options = []
        for n in match_config.SHORTLIST_CHOICES:
            calls = math.ceil(match_config.pool_size(n) / match_config.RERANK_BATCH_SIZE)
            minutes = max(1, round(calls * SECONDS_PER_LLM_CALL / 60))
            shortlist_options.append((n, calls, minutes))
        return render(
            request, "match_candidate.html",
            cid=candidate_id, p=profile, suggestions=suggest_roles(profile), roles=ROLE_CHOICES,
            runs=runs, shortlist_options=shortlist_options, llm_on=bool(config.GEMINI_API_KEY),
        )

    @router.post("/match/candidates/{candidate_id}/run")
    async def candidate_run(request: Request, candidate_id: int, conn=Depends(get_conn)):
        load_candidate(conn, candidate_id)  # 404 if unknown
        form = await request.form()
        args = build_match_args(candidate_id, form, int_or)
        if "--no-rerank" not in args and not config.GEMINI_API_KEY:
            raise HTTPException(400, "The LLM rerank needs a GEMINI_API_KEY; tick 'quick mode' instead.")
        uid = auth.user_id(request)
        if uid and not auth.is_admin(request):  # the shared free Gemini key: a fair share per student
            from ..outreach.run import start_of_today

            if db.user_match_runs_since(conn, uid, start_of_today()) >= config.MAX_MATCH_RUNS_PER_DAY:
                raise HTTPException(429, f"That's {config.MAX_MATCH_RUNS_PER_DAY} match runs today, the daily "
                                         "limit. Your shortlists are saved; try again tomorrow.")
        start_task("Match", [args], uid)
        return RedirectResponse(f"/match/task?next=/match/candidates/{candidate_id}/latest", status_code=303)

    @router.post("/match/candidates/{candidate_id}/forget")
    async def candidate_forget(candidate_id: int, conn=Depends(get_conn)):
        """Delete this resume and everything made from it (runs, drafts, outreach)."""
        from .runner import run_now

        load_candidate(conn, candidate_id)
        code, out = await run_now(["forget", "--candidate", str(candidate_id), "--yes"])
        if code != 0:
            raise HTTPException(500, "Could not delete it: " + (out.strip().splitlines() or ["?"])[-1][:200])
        return RedirectResponse("/match", status_code=303)

    @router.get("/match/candidates/{candidate_id}/latest")
    def candidate_latest(candidate_id: int, conn=Depends(get_conn)):
        run_id = db.latest_run_id(conn, candidate_id)
        return RedirectResponse(f"/match/runs/{run_id}" if run_id else f"/match/candidates/{candidate_id}", status_code=303)

    # ---------- results ----------

    def run_rows(conn, run_id: int, n: int | None):
        run = db.get_run(conn, run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        prefs = Preferences.model_validate_json(run["prefs_json"])
        rows = db.run_matches(conn, run_id)
        return run, prefs, rows, rows[: n or prefs.shortlist]

    @router.get("/match/runs/{run_id}")
    def run_page(request: Request, run_id: int, conn=Depends(get_conn)):
        n = int_or(request.query_params.get("n"), None, 1, match_config.MAX_POOL + 100)
        run, prefs, everything, rows = run_rows(conn, run_id, n)
        from ..tracker.metrics import feedback

        return render(
            request, "match_run.html", run=run, prefs=prefs, rows=rows, pool=len(everything),
            filter_stats=json.loads(run["filter_stats"]),
            outreach=db.outreach_status_by_company(conn, run["candidate_id"]),  # Part 4 feedback
            feedback=feedback(conn, run["candidate_id"]),
        )

    @router.get("/match/runs/{run_id}/shortlist.csv")
    def run_csv(run_id: int, conn=Depends(get_conn)):
        _, _, _, rows = run_rows(conn, run_id, None)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["rank", "company", "score", "role", "posted", "link", "website", "contact", "why it fits", "red flags"])
        for i, m in enumerate(rows, 1):
            w.writerow([
                i, m["company_name"], m["final_score"], m["job_title"] or m["pitch_role"] or "",
                "yes" if m["job_title"] else "no", m["apply_url"] or m["careers_url"] or "", m["website"] or "",
                m["contact"] or "", m["why_fit"] or "", "; ".join(m["red_flags"]),
            ])
        return Response(out.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="sort-sherr-shortlist-run{run_id}.csv"'})

    # ---------- live task ----------

    @router.get("/match/task")
    def task_page(request: Request):
        nxt = request.query_params.get("next", "/match")
        if not re.match(r"^/match/[\w./-]*$", nxt):  # only jump to our own pages
            nxt = "/match"
        return render(request, "match_task.html", task=match_task(request), next=nxt)

    @router.get("/match/task.json")
    def task_json(request: Request):
        """What the progress page polls. Without the admin password we only show Match tasks."""
        return {"busy": runner.busy, "task": match_task(request)}

    @router.post("/match/stop")
    async def task_stop(request: Request):
        if match_task(request) and runner.busy:  # can't stop an admin crawl (or another student's task)
            await runner.stop()
        return RedirectResponse("/match/task", status_code=303)

    return router


def match_task(request: Request) -> dict | None:
    """The current runner task, only if it's a Match task AND (hosted) this student's own: its log
    shows their profile and companies."""
    t = runner.current
    if not t or t.title not in MATCH_TASKS:
        return None
    if config.LOGIN_REQUIRED and not auth.is_admin(request) and t.owner != auth.user_id(request):
        return None
    return t.to_dict()


def start_task(title: str, commands: list[list[str]], owner: int | None = None) -> None:
    try:
        runner.start(title, commands, owner)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


def build_match_args(candidate_id: int, form, int_or) -> list[str]:
    """Form -> `match` CLI arguments. Every value is checked; free text (interests) is passed as ONE
    argument to exec (never a shell), so it can't run anything."""
    shortlist = int_or(form.get("shortlist"), 50, 1, 1_000_000)
    if shortlist not in match_config.SHORTLIST_CHOICES:
        shortlist = 50
    args = ["match", "--candidate", str(candidate_id), "--top", str(shortlist)]
    for role in form.getlist("role"):
        if role in ROLE_CHOICES and role != "any":
            args += ["--role", role]
    if form.get("remote_only"):
        args.append("--remote-only")
    for loc in (form.get("locations") or "").split(",")[:5]:
        loc = loc.strip()
        if loc and LOCATION.match(loc):
            args.append(f"--location={loc}")
    if form.get("paid_only"):
        args.append("--paid-only")
    window = (form.get("window") or "").strip()
    if window and WINDOW.match(window):
        args.append(f"--window={window}")
    interests = "".join(ch for ch in (form.get("interests") or "") if ch.isprintable() or ch == "\n").strip()[:1000]
    if interests:
        args.append(f"--interests={interests}")  # "=": text starting with "-" stays text
    if form.get("quick"):
        args.append("--no-rerank")
    return args
