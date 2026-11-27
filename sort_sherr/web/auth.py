"""Student accounts for the hosted site. Locally (LOGIN_REQUIRED off) none of this is active.

  - Invite-only: the admin makes a one-time link (/join/<code>). There is no email sign-up or
    "forgot password" email, because Sort Sherr has no email-sending code at all.
  - Passwords: hashed with scrypt (Python's standard library), never stored or logged as text.
  - Login: a signed cookie (Starlette's SessionMiddleware) holding only the user id.
  - Every /match page goes through owner_guard: a student only ever sees their own resumes,
    shortlists, drafts and outreach (anything else is a 404). The admin sees everything.

Why these routes write to the database directly (unlike admin tasks, which run CLI commands):
a password must never become a command-line argument, where other processes could read it.
"""

import hashlib
import hmac
import re
import secrets
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import config
from ..storage import db

USERNAME = re.compile(r"^[a-zA-Z0-9_.-]{3,30}$")
MIN_PASSWORD = 10
PUBLIC_PATHS = ("/login", "/join/", "/static/", "/healthz")
MAX_FAILURES, LOCK_SECONDS = 5, 300

# ---------- passwords ----------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        mine = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    except ValueError:
        return False
    return hmac.compare_digest(mine.hex(), digest)  # constant time: no hints from timing


def password_problem(password: str, again: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if password != again:
        return "The two passwords don't match."
    return None


def new_invite_code() -> tuple[str, str]:
    """(code for the link, its hash for the database)."""
    code = secrets.token_urlsafe(18)
    return code, code_hash(code)


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# ---------- who is logged in ----------


def user_id(request: Request) -> int | None:
    """The logged-in user's id, or None (also None when login is off: local single-user use)."""
    return request.session.get("uid") if config.LOGIN_REQUIRED else None


def is_admin(request: Request) -> bool:
    return not config.LOGIN_REQUIRED or bool(request.session.get("admin"))


async def login_gate(request: Request, call_next):
    """HTTP middleware: when hosted, every page except login/join/static needs a session."""
    path = request.url.path
    if config.LOGIN_REQUIRED and not request.session.get("uid") and not path.startswith(PUBLIC_PATHS):
        if request.method == "GET" and not path.endswith(".json"):
            return RedirectResponse(f"/login?next={path}", status_code=303)
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "Please log in"}, status_code=401)
    return await call_next(request)


def owner_guard(get_conn):
    """A router dependency: the candidate in the URL (cid / candidate_id, or the run's candidate)
    must belong to the logged-in student. One check for every Match/Draft/Outreach page, so a new
    route can't forget it."""

    def guard(request: Request, conn=Depends(get_conn)):
        if not config.LOGIN_REQUIRED or request.session.get("admin"):
            return
        pp = request.path_params
        cid = pp.get("cid", pp.get("candidate_id"))
        if "run_id" in pp:
            run = db.get_run(conn, int(pp["run_id"]))
            cid = run["candidate_id"] if run else -1
        if cid is None:
            return
        row = db.get_candidate(conn, int(cid))
        if row is None or row["user_id"] != request.session.get("uid"):
            raise HTTPException(404, "Not found")  # 404, not 403: don't confirm it exists

    return guard


# ---------- pages ----------

router = APIRouter()
failures: dict[str, tuple[int, float]] = {}  # username -> (failed attempts, locked until): slows guessing


def setup(templates, get_conn):
    def render(request, name, **ctx):
        return templates.TemplateResponse(request, name, {"active": "", **ctx})

    def safe_next(value: str | None) -> str:
        return value if value and re.match(r"^/[\w/?=&.-]*$", value) and not value.startswith("//") else "/"

    @router.get("/healthz")
    def healthz():
        return {"ok": True}

    @router.get("/login")
    def login_page(request: Request):
        return render(request, "login.html", next=safe_next(request.query_params.get("next")), error=None)

    @router.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/"),
              conn=Depends(get_conn)):
        key = username.strip().lower()
        count, until = failures.get(key, (0, 0.0))
        if until > time.time():
            return render(request, "login.html", next=safe_next(next),
                          error="Too many wrong passwords. Wait a few minutes and try again.")
        user = db.get_user_by_name(conn, username.strip())
        if user is None or not check_password(password, user["password_hash"]):
            count += 1
            failures[key] = (count, time.time() + LOCK_SECONDS if count >= MAX_FAILURES else 0.0)
            return render(request, "login.html", next=safe_next(next), error="Wrong username or password.")
        failures.pop(key, None)
        request.session.clear()  # a fresh session on every login
        request.session.update({"uid": user["id"], "name": user["username"], "admin": bool(user["is_admin"])})
        return RedirectResponse(safe_next(next), status_code=303)

    @router.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @router.get("/join/{code}")
    def join_page(request: Request, code: str, conn=Depends(get_conn)):
        invite = db.get_invite(conn, code_hash(code))
        if invite is None:
            raise HTTPException(404, "This link was already used or doesn't exist. Ask for a new one.")
        reset_for = db.get_user(conn, invite["for_user_id"])
        return render(request, "join.html", code=code, reset_for=reset_for, error=None)

    @router.post("/join/{code}")
    def join(request: Request, code: str, username: str = Form(""), password: str = Form(...),
             again: str = Form(...), conn=Depends(get_conn)):
        invite = db.get_invite(conn, code_hash(code))
        if invite is None:
            raise HTTPException(404, "This link was already used or doesn't exist. Ask for a new one.")
        reset_for = db.get_user(conn, invite["for_user_id"])
        error = password_problem(password, again)
        if not error and not reset_for:
            username = username.strip()
            if not USERNAME.match(username):
                error = "Username: 3-30 letters, digits, dots, dashes or underscores."
            elif db.get_user_by_name(conn, username):
                error = "That username is taken."
        if error:
            return render(request, "join.html", code=code, reset_for=reset_for, error=error)
        if reset_for:
            db.set_password(conn, reset_for["id"], hash_password(password))
            uid = reset_for["id"]
        else:
            uid = db.create_user(conn, username, hash_password(password))
        db.use_invite(conn, code_hash(code), uid)
        user = db.get_user(conn, uid)
        request.session.clear()
        request.session.update({"uid": uid, "name": user["username"], "admin": bool(user["is_admin"])})
        return RedirectResponse("/match", status_code=303)

    return router
