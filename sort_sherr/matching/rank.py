"""Step B4: the final score. Mostly the LLM's rubric score, plus small boosts.

    final = W_RERANK * rerank_score            (0-100, the LLM rubric)
          + W_RETRIEVAL * retrieval_norm * 100 (embedding similarity, scaled 0-1 within this run's pool)
          + recency boost                      (recent YC batch = recent funding; up to BOOST_RECENT_BATCH)
          + intern boost                       (an active internship posting, not PhD-only; BOOST_INTERN_POSTING)

All weights live in match_config.py. Without a rerank (`match --no-rerank`) the final score is
just the retrieval similarity, so you see the pure embedding order.
"""

from .. import match_config as cfg
from ..scoring import recency_points
from .models import GRAD_ONLY


def normalize(scores: list[float]) -> list[float]:
    """Min-max to 0-1 so similarities (which cluster around e.g. 0.5-0.7) become comparable to 0-100."""
    lo, hi = min(scores, default=0), max(scores, default=0)
    return [(s - lo) / (hi - lo) if hi > lo else 1.0 for s in scores]


def boosts(company: dict) -> float:
    recency = recency_points(company.get("yc_batch")) / 40 * cfg.BOOST_RECENT_BATCH  # recency_points is 0-40
    # PhD/master's-only internships don't count: our students are (mostly) undergrads
    open_intern = any(j["is_internship"] == 1 and not GRAD_ONLY.search(j.get("title") or "") for j in company.get("jobs", []))
    intern = cfg.BOOST_INTERN_POSTING if open_intern else 0
    return recency + intern


def final_score(rerank_score: int | None, retrieval_norm: float, company: dict) -> float:
    if rerank_score is None:
        return round(retrieval_norm * 100, 1)
    return round(cfg.W_RERANK * rerank_score + cfg.W_RETRIEVAL * retrieval_norm * 100 + boosts(company), 1)
