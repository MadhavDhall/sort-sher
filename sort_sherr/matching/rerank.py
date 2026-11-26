"""Step B3: the LLM reads the student's profile and a few company cards at a time, and scores
each company on a fixed rubric, citing evidence from BOTH sides.

Why batches of 5? Fewer calls (the free tier allows ~10/min) while each company still gets
enough attention. Answers are cached, so re-running `match` with the same profile, preferences
and company data costs nothing.

Guards against vague or made-up reasons:
  - each evidence item must pair a resume item with a company fact
  - an evidence item whose company fact shares no word with the company card is dropped
  - best_job_id must be one of the company's listed job ids, else it's cleared
"""

import hashlib
import json
import re

from .. import match_config
from ..storage import db
from .models import GRAD_ONLY, CandidateProfile, Preferences, RerankBatch, RerankResult
from .retrieve import candidate_document

PROMPT = """You help a college student find a startup internship. Score how well each company fits THIS student.

STUDENT
{student}

STUDENT PREFERENCES
{prefs}

COMPANIES
{cards}

For EACH company return one result with these scores (integers 0-20):
- stack_overlap: the student's languages/frameworks/tools vs. what the company uses or asks for in its roles.
- domain_fit: how close the student's projects/experience are to what the company builds.
- interest_fit: how well the company matches what the student said they want (10 if they said nothing;
  low if it is something they said to avoid).
- seniority_fit: would this company plausibly take an intern or new grad? (tiny team with only senior
  roles = low; has intern/junior roles or is a growing early-stage team = high). Roles only open to
  PhD / master's students, or needing years of experience, do NOT fit an undergraduate: if those are
  the only roles listed, seniority_fit is 0-5.
- hiring_signal: 18-20 = an internship the student is eligible for; 12-16 = engineering postings that
  fit the student; 6-10 = only marked "hiring" / unrelated postings; 0-4 = no sign of hiring.
  An internship only for PhD / master's students counts as an unrelated posting for an undergraduate.

Also:
- why_fit: 1-2 plain sentences to the student, naming one of THEIR projects/skills and one specific
  thing the company does. Example: "You built a RAG search with LangChain; they build retrieval tools
  for lawyers and want Python engineers."
- evidence: 2-3 items. resume_item = a specific project/skill/experience from the student (or their stated
  interest); company_fact = a specific fact copied from that company's card about WHAT THEY BUILD
  (product, technology, customers, tag, job title, requirement). Never use team size, location,
  YC batch or hiring status as a company_fact: those are not reasons. No generic praise ("great
  culture", "fast-growing", "exciting mission").
- best_job_id: the id of the single best-fitting listed job for this student, or null if none fits.
  Never pick a PhD / master's-only role or a senior role (years of experience) for an undergraduate.
- why_this_role: one sentence on why that job suits the student (null if best_job_id is null).
- pitch_role: the internship role the student should ask for in a cold email, e.g. "Backend engineering
  intern" or "Applied AI intern (RAG / agents)". Base it on what the company builds and the student's
  strengths. Always fill it, even when no job is posted (most startups take interns without posting).
- red_flags: concrete concerns, e.g. "only senior roles", "team of 2", "not remote but student wants remote",
  "no mention of paid internships" (only if the student asked for paid), "crypto, which the student wants to avoid".
Use company_id exactly as given. Only use facts present above; never invent."""


def student_text(profile: CandidateProfile) -> str:
    edu = "; ".join(
        ", ".join(x for x in [e.degree, e.branch, e.college, e.grad_date and f"graduating {e.grad_date}"] if x)
        for e in profile.education
    )
    parts = [f"Education: {edu}" if edu else "", candidate_document(profile)]
    if profile.achievements:
        parts.append("Achievements: " + "; ".join(profile.achievements[:5]))
    return "\n".join(p for p in parts if p)


def prefs_text(prefs: Preferences) -> str:
    lines = [
        f"Roles wanted: {', '.join(prefs.roles)}",
        f"Remote only: {'yes' if prefs.remote_only else 'no'}",
        f"Locations: {', '.join(prefs.locations) or 'anywhere'}",
        f"Paid only: {'yes' if prefs.paid_only else 'no'}",
        f"Time window: {prefs.window or 'flexible'}",
        f"In their own words: {prefs.interests or '(nothing given)'}",
    ]
    return "\n".join(lines)


def company_card(c: dict) -> str:
    """What the LLM sees about one company. Jobs carry their database id so it can pick best_job_id."""
    lines = [f"company_id: {c['id']}", f"name: {c['name']}"]
    if c.get("description"):
        lines.append(f"about: {' '.join(c['description'].split())[:600]}")
    for label, value in [
        ("tags", ", ".join(c.get("tags") or [])),
        ("yc_batch", c.get("yc_batch")),
        ("team_size", c.get("team_size")),
        ("locations", ", ".join(c.get("locations") or [])),
        ("marked_hiring", {1: "yes", 0: "no"}.get(c.get("is_hiring"), "unknown")),
    ]:
        if value:
            lines.append(f"{label}: {value}")
    jobs = c.get("jobs", [])[: match_config.MAX_JOBS_IN_DOC]
    if not jobs:
        lines.append("open jobs: none found")
    for j in jobs:
        flags = [x for x in [j["is_internship"] == 1 and "internship", j["remote"] == 1 and "remote", j["location"]] if x]
        req = " ".join((j.get("requirements") or j.get("description") or "").split())[:200]
        lines.append(f"- job {j['id']}: {j['title']} ({', '.join(flags) or 'details unknown'}) {req}".rstrip())
    return "\n".join(lines)


WORD = re.compile(r"[a-z0-9+#.]{3,}")
STOP = {"the", "and", "for", "with", "they", "their", "are", "that", "this", "from", "has", "have", "build", "builds", "building"}


def _words(text: str) -> set[str]:
    return {w.strip(".") for w in WORD.findall(text.lower())} - STOP


# Card lines that describe the company's situation, not what it builds. "team_size: 4" or a city is
# never a reason to email a company, so evidence that only cites these is dropped.
META_FIELDS = ("yc_batch:", "team_size:", "locations:", "marked_hiring:")


def clean_result(r: RerankResult, card: str, student: str, jobs: dict[int, str], student_is_grad: bool) -> RerankResult:
    """Code checks that don't rely on the LLM following the prompt:
    - evidence must quote both inputs, and the company side must be about what they build
    - best_job_id must be one of the company's jobs, and not a PhD/master's-only role for an undergrad"""
    lines = card.splitlines()
    meta_words = _words("\n".join(l for l in lines if l.startswith(META_FIELDS)))
    content_words = _words("\n".join(l for l in lines if not l.startswith(META_FIELDS)))
    student_words = _words(student)

    def grounded(e) -> bool:
        fact = _words(e.company_fact)
        return bool(fact & content_words) and not fact <= meta_words and bool(_words(e.resume_item) & student_words)

    r.evidence = [e for e in r.evidence if grounded(e)]
    if r.best_job_id not in jobs:
        r.best_job_id, r.why_this_role = None, None
    elif not student_is_grad and GRAD_ONLY.search(jobs[r.best_job_id]):
        r.red_flags.append(f"only for PhD/master's students: {jobs[r.best_job_id]}")
        r.best_job_id, r.why_this_role = None, None
    return r


def cache_key(model: str, student: str, prefs: str, card: str) -> str:
    # PROMPT is part of the key: if you change the rubric, old cached answers must not be reused.
    return hashlib.sha256(f"{PROMPT}\n{model}\n{student}\n{prefs}\n{card}".encode()).hexdigest()


async def rerank(conn, provider, profile: CandidateProfile, prefs: Preferences, companies: list[dict], progress=None):
    """Score every company. Returns ({company_id: RerankResult}, usage dict)."""
    model = getattr(provider, "model", "unknown")
    student, pref = student_text(profile), prefs_text(prefs)
    student_all = student + "\n" + (prefs.interests or "")
    cards = {c["id"]: company_card(c) for c in companies}
    jobs = {c["id"]: {j["id"]: j["title"] for j in c.get("jobs", [])} for c in companies}
    student_is_grad = any(GRAD_ONLY.search(e.degree or "") for e in profile.education)
    usage = {"llm_calls": 0, "prompt_tokens": 0, "output_tokens": 0, "cached": 0}

    results: dict[int, RerankResult] = {}
    todo = []
    for c in companies:
        hit = db.rerank_cache_get(conn, cache_key(model, student, pref, cards[c["id"]]))
        if hit:
            results[c["id"]] = RerankResult.model_validate_json(hit)
            usage["cached"] += 1
        else:
            todo.append(c["id"])

    size = match_config.RERANK_BATCH_SIZE
    batches = [todo[i : i + size] for i in range(0, len(todo), size)]
    for n, batch in enumerate(batches, 1):
        if progress:
            progress(f"  rerank batch {n}/{len(batches)} ({len(batch)} companies)")
        for attempt in range(2):  # second try only for companies the LLM skipped
            prompt = PROMPT.format(student=student, prefs=pref, cards="\n\n".join(cards[i] for i in batch))
            answer, used = await provider.generate(prompt, RerankBatch)
            usage["llm_calls"] += 1
            usage["prompt_tokens"] += used.get("prompt_tokens", 0)
            usage["output_tokens"] += used.get("output_tokens", 0)
            for r in answer.results:
                if r.company_id in batch and r.company_id not in results:
                    r = clean_result(r, cards[r.company_id], student_all, jobs[r.company_id], student_is_grad)
                    results[r.company_id] = r
                    db.rerank_cache_put(conn, cache_key(model, student, pref, cards[r.company_id]), r.model_dump_json())
            batch = [i for i in batch if i not in results]
            if not batch:
                break
    return results, usage


def usage_cost(usage: dict) -> float:
    """What these tokens would cost on the paid tier (the free tier is $0)."""
    return (
        usage["prompt_tokens"] * match_config.PRICE_INPUT_PER_M
        + usage["output_tokens"] * match_config.PRICE_OUTPUT_PER_M
    ) / 1_000_000


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False)
