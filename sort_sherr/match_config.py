"""Every number that controls Part 2 (matching) lives here, so you can tune it in one place."""

import os

# ---------- retrieval (cheap, local embeddings) ----------
EMBED_MODEL = os.getenv("SORT_SHERR_EMBED_MODEL", "BAAI/bge-small-en-v1.5")  # free, local, 384-dim
EMBEDDER = os.getenv("SORT_SHERR_EMBEDDER", "sentence-transformers")  # or "fastembed" (no torch: the hosted site)
TOP_K_RESUME = 75  # companies kept by resume similarity (at least; grows with the shortlist size)
POOL_FACTOR = 1.5  # rerank 1.5x the shortlist, so the LLM can push weak ones out (shortlist 100 -> pool 150)
MAX_POOL = 300  # cap: 300 companies = 60 LLM calls (~8-10 min on the free tier)
TOP_K_INTERESTS = 25  # extra companies kept by similarity to the student's own "interests" text
JOB_REQ_CHARS = 300  # how much of each job's requirements goes into the company document
MAX_JOBS_IN_DOC = 8  # a company with 60 open roles shouldn't drown out its own description

# ---------- rerank (LLM) ----------
RERANK_BATCH_SIZE = 5  # companies per LLM call; 100 companies -> 20 calls -> ~2 min on the free tier

# ---------- final score ----------
# final = W_RERANK * rerank_score + W_RETRIEVAL * (retrieval similarity scaled to 0-100) + boosts
W_RERANK = 0.85
W_RETRIEVAL = 0.10
BOOST_RECENT_BATCH = 5  # max points for a recent YC batch (scaled from scoring.recency_points)
BOOST_INTERN_POSTING = 5  # the company has an active internship posting right now

# ---------- results ----------
TOP_N = 15  # default shortlist size
SHORTLIST_CHOICES = [15, 30, 50, 75, 100, 150]  # what the website offers; off-campus needs a long list


def pool_size(shortlist: int) -> int:
    """How many companies retrieval keeps (by resume) for a shortlist of this size."""
    return min(MAX_POOL, max(TOP_K_RESUME, round(POOL_FACTOR * shortlist)))

# ---------- cost logging ----------
# The free tier costs $0. We also show what the same run WOULD cost on the paid tier, so you can
# judge whether the rerank is worth it. USD per 1M tokens; check ai.google.dev/pricing for the
# current numbers for your model.
PRICE_INPUT_PER_M = float(os.getenv("SORT_SHERR_PRICE_INPUT_PER_M", "0.10"))
PRICE_OUTPUT_PER_M = float(os.getenv("SORT_SHERR_PRICE_OUTPUT_PER_M", "0.40"))
