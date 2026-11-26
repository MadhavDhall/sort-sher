"""One `match` run, start to finish:

    profile + preferences
      -> hard filters      (retrieve.hard_filter)    ~4,600 -> a few thousand companies
      -> retrieval         (retrieve.retrieve)       -> ~75-100 companies, by embedding similarity
      -> rerank (LLM)      (rerank.rerank)           -> rubric score + reasons for each
      -> final score       (rank.final_score)
      -> saved as one row in match_runs + one row per pool company in matches
"""

import json

from ..storage import db
from . import rank
from .models import CandidateProfile, Preferences
from .rerank import dumps, rerank, usage_cost
from .retrieve import hard_filter, retrieve


async def run_match(conn, embedder, provider, candidate_id: int, prefs: Preferences, progress=print) -> int:
    """provider=None -> retrieval only (no LLM). Returns the new run id."""
    row = db.get_candidate(conn, candidate_id)
    if row is None:
        raise ValueError(f"no candidate #{candidate_id}; run parse-resume first")
    profile = CandidateProfile.model_validate_json(row["profile_json"])

    companies = db.matching_companies(conn)
    kept, filter_stats = hard_filter(companies, prefs)
    progress(f"Filters: {len(companies)} companies -> {len(kept)} kept {json.dumps(filter_stats)}")

    pool, newly_embedded = retrieve(conn, embedder, profile, prefs, kept)
    progress(f"Retrieval: {len(pool)} companies in the pool ({newly_embedded} documents newly embedded)")

    results, usage = {}, {"llm_calls": 0, "prompt_tokens": 0, "output_tokens": 0, "cached": 0}
    if provider is not None and pool:
        results, usage = await rerank(conn, provider, profile, prefs, [m["company"] for m in pool], progress)

    norms = rank.normalize([m["retrieval_score"] for m in pool])
    rows = []
    for m, norm in zip(pool, norms):
        c, r = m["company"], results.get(m["company"]["id"])
        rows.append({
            "company_id": c["id"],
            "job_id": r.best_job_id if r else None,
            "found_by": m["found_by"],
            "retrieval_score": round(m["retrieval_score"], 4),
            "rerank_score": r.total() if r else None,
            "final_score": rank.final_score(r.total() if r else None, norm, c),
            "sub_scores": dumps(r.sub_scores() if r else {}),
            "why_fit": r.why_fit if r else None,
            "why_this_role": r.why_this_role if r else None,
            "pitch_role": r.pitch_role if r else None,
            "reasons": dumps([e.model_dump() for e in r.evidence] if r else []),
            "red_flags": dumps(r.red_flags if r else []),
        })

    run = {
        "prefs_json": prefs.model_dump_json(),
        "filter_stats": json.dumps(filter_stats),
        "embed_model": embedder.name,
        "llm_model": getattr(provider, "model", None),
        "reranked": int(provider is not None),
        "llm_calls": usage["llm_calls"],
        "prompt_tokens": usage["prompt_tokens"],
        "output_tokens": usage["output_tokens"],
        "cost_usd": round(usage_cost(usage), 6),
    }
    if provider is not None:
        progress(
            f"LLM: {usage['llm_calls']} calls, {usage['cached']} cached, "
            f"{usage['prompt_tokens']:,} in + {usage['output_tokens']:,} out tokens"
        )
    return db.save_match_run(conn, candidate_id, run, rows)
