"""Step B1 + B2: hard filters, then cheap retrieval with embeddings.

Why company-level? Many startups take interns without posting an intern role, so we match the
student to the COMPANY (what it builds, its stack) and use job postings as extra signal.

Filters only drop a company when we KNOW it doesn't fit. Unknown (no jobs, no location...) is kept,
and counted, so `match` can show how many were kept only because we didn't know.
"""

import re

import numpy as np

from .. import match_config
from .embed import Embedder, embed_cached
from .models import CandidateProfile, Preferences

# Job-title keywords per role choice. A company passes the role filter if ANY active job matches,
# or if it has no jobs at all (unknown).
ROLE_PATTERNS = {
    "sde": r"engineer|developer|swe|software|programmer",
    "backend": r"back.?end|platform|infra|api|server|distributed|software engineer",
    "frontend": r"front.?end|ui\b|web|react|software engineer",
    "fullstack": r"full.?stack|software engineer|product engineer",
    "ai-ml": r"\bai\b|\bml\b|machine learning|llm|research|applied scien|data scien|nlp|vision|deep learning",
    "data": r"data|analytics|analyst|ml\b",
    "mobile": r"mobile|ios|android|react native|flutter",
    "devops": r"devops|sre|reliability|infra|platform|cloud",
    "design": r"design|ux|ui\b",
    "product": r"product manager|\bpm\b|product",
}


# ---------- text documents ----------


def company_document(c: dict) -> str:
    """Everything we know about what the company builds and who it's hiring, as one text."""
    parts = [c["name"] + "."]
    if c.get("description"):
        parts.append(c["description"])
    if c.get("tags"):
        parts.append("Tags: " + ", ".join(c["tags"]) + ".")
    jobs = c.get("jobs", [])[: match_config.MAX_JOBS_IN_DOC]
    if jobs:
        parts.append("Open roles: " + "; ".join(j["title"] for j in jobs) + ".")
        reqs = [(j.get("requirements") or j.get("description") or "")[: match_config.JOB_REQ_CHARS] for j in jobs[:3]]
        parts.extend(" ".join(r.split()) for r in reqs if r)
    return "\n".join(parts)


def candidate_document(p: CandidateProfile) -> str:
    """What the student has done and can do. (No name/links: not useful for matching.)"""
    parts = []
    if p.focus_areas:
        parts.append("Focus: " + ", ".join(p.focus_areas) + ".")
    if p.skills:
        parts.append("Skills: " + ", ".join(p.skills) + ".")
    for pr in p.projects:
        parts.append(f"Project {pr.title}: {pr.description or ''} ({', '.join(pr.tech_stack)})")
    for e in p.experience:
        parts.append(f"{e.role} at {e.organization or ''}: {e.description or ''} ({', '.join(e.tech_stack)})")
    return "\n".join(parts)


# ---------- hard filters ----------


def _has_remote_signal(c: dict) -> bool:
    return any(j["remote"] == 1 for j in c["jobs"]) or any("remote" in loc.lower() for loc in c["locations"])


def filter_reason(c: dict, prefs: Preferences) -> tuple[str, str]:
    """Returns ("keep" | "keep_unknown" | "drop", reason). One reason per company, checked in order."""
    unknown = []

    # 1. hiring signal: drop only if YC says not hiring AND we found no jobs AND no HN post
    if not c["jobs"] and "hn" not in c["sources"]:
        if c["is_hiring"] == 0:
            return "drop", "not hiring"
        if c["is_hiring"] is None:
            unknown.append("hiring")

    # 2. remote only: drop only when every active job is known to be on-site
    if prefs.remote_only and not _has_remote_signal(c):
        if c["jobs"] and all(j["remote"] == 0 for j in c["jobs"]):
            return "drop", "on-site only"
        unknown.append("remote")

    # 3. locations: drop only on a known mismatch (company/job locations known, none match, no remote)
    if prefs.locations:
        known = [loc for loc in c["locations"] + [j["location"] or "" for j in c["jobs"]] if loc]
        wanted = [w.lower() for w in prefs.locations]
        if not known:
            unknown.append("location")
        elif not any(w in loc.lower() for loc in known for w in wanted) and not _has_remote_signal(c):
            return "drop", "location"

    # 4. role type: drop only if the company has jobs and none fits the wanted roles (or is an internship)
    roles = [r for r in prefs.roles if r != "any"]
    if roles and c["jobs"]:
        pattern = re.compile("|".join(ROLE_PATTERNS[r] for r in roles), re.I)
        if not any(pattern.search(j["title"]) or j["is_internship"] == 1 for j in c["jobs"]):
            return "drop", "role"

    return ("keep_unknown", "unknown " + "+".join(unknown)) if unknown else ("keep", "")


def hard_filter(companies: list[dict], prefs: Preferences) -> tuple[list[dict], dict]:
    """Returns (companies kept, counts like {"kept": .., "kept_unknown": .., "dropped: role": ..})."""
    kept, stats = [], {"kept": 0, "kept_unknown": 0}
    for c in companies:
        verdict, reason = filter_reason(c, prefs)
        if verdict == "drop":
            stats[f"dropped: {reason}"] = stats.get(f"dropped: {reason}", 0) + 1
        else:
            stats["kept" if verdict == "keep" else "kept_unknown"] += 1
            kept.append(c)
    return kept, stats


# ---------- retrieval ----------


def retrieve(conn, embedder: Embedder, profile: CandidateProfile, prefs: Preferences, companies: list[dict]) -> tuple[list[dict], int]:
    """Top companies by cosine similarity. Returns (pool, newly embedded count).

    Pool = top pool_size(prefs.shortlist) by similarity to the resume (>= TOP_K_RESUME)
         + top TOP_K_INTERESTS by similarity to the student's interests text (if given), deduped.
    Each pool item: {"company": dict, "doc": str, "retrieval_score": float, "found_by": str}."""
    if not companies:
        return [], 0
    docs = [company_document(c) for c in companies]
    doc_vecs, new = embed_cached(conn, embedder, docs)

    resume_vec, _ = embed_cached(conn, embedder, [candidate_document(profile)], is_query=True)
    resume_sim = doc_vecs @ resume_vec[0]  # cosine similarity (all vectors are normalized)
    k = match_config.pool_size(prefs.shortlist)
    found_by = {int(i): "resume" for i in np.argsort(-resume_sim)[:k]}
    score = resume_sim

    if prefs.interests:
        interest_vec, _ = embed_cached(conn, embedder, [prefs.interests], is_query=True)
        interest_sim = doc_vecs @ interest_vec[0]
        for i in np.argsort(-interest_sim)[: match_config.TOP_K_INTERESTS]:
            found_by[int(i)] = "both" if int(i) in found_by else "interests"
        score = np.maximum(resume_sim, interest_sim)  # good on either side counts

    pool = [
        {"company": companies[i], "doc": docs[i], "retrieval_score": float(score[i]), "found_by": how}
        for i, how in found_by.items()
    ]
    pool.sort(key=lambda m: m["retrieval_score"], reverse=True)
    return pool, new
