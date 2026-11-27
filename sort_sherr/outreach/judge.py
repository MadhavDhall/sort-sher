"""Do the drafts look good to a fixed rubric AND to you? And does the judge agree with you?

  1. `eval-drafts --candidate N`: an LLM judge scores up to 10 drafts on 5 criteria (1-5 each,
     rubric in prompts/judge.md) and writes labels/draft_ratings.csv with an empty `my_rating` column.
  2. You fill `my_rating` (1-5) for each draft in the CSV.
  3. `eval-drafts --candidate N --report`: judge average vs your rating, per draft and overall:
     mean absolute difference, how many are within 1 point, and the correlation.
Re-running step 1 keeps the ratings you already wrote.
"""

import csv
from pathlib import Path

from ..storage import db
from . import fill, load_prompt
from .models import JudgeScores

CRITERIA = ("specificity", "grounding", "relevance", "length", "tone")
COLUMNS = ["draft_id", "company", "variant", *CRITERIA, "judge_avg", "judge_comment", "my_rating"]


def pick_drafts(conn, candidate_id: int, n: int) -> list[dict]:
    """Approved drafts first (that's what you'd actually send), then the rest, oldest first."""
    drafts = db.candidate_drafts(conn, candidate_id)
    return sorted(drafts, key=lambda d: (not d["chosen"], d["id"]))[:n]


async def judge_draft(provider, d: dict) -> tuple[JudgeScores, dict]:
    facts = "\n".join(f"{f['id']}: {f['text']}" for f in d["facts_used"]) or "(none cited)"
    items = "\n".join(f"{i['id']}: {i['text']}" for i in d["student_items_used"]) or "(none cited)"
    prompt = fill(load_prompt("judge.md"), facts=facts, student=items, subject=d["subject"],
                  body=d["edited_body"] or d["body"])
    return await provider.generate(prompt, JudgeScores)


def read_my_ratings(path: Path) -> dict[int, str]:
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {int(r["draft_id"]): (r.get("my_rating") or "").strip() for r in csv.DictReader(f) if r.get("draft_id", "").isdigit()}


def write_csv(path: Path, rows: list[dict]) -> None:
    """rows: one dict per draft with the COLUMNS keys. Keeps ratings you already wrote."""
    mine = read_my_ratings(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({**r, "my_rating": mine.get(r["draft_id"], r.get("my_rating", ""))})


def judge_rows(drafts: list[dict], scores: list[JudgeScores]) -> list[dict]:
    rows = []
    for d, s in zip(drafts, scores):
        rows.append({"draft_id": d["id"], "company": d["company_name"], "variant": d["variant"],
                     **{c: getattr(s, c) for c in CRITERIA}, "judge_avg": s.average(), "judge_comment": s.comment,
                     "my_rating": ""})
    return rows


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def report(path: Path) -> dict:
    """Compare the judge with your ratings. Only rows where you wrote a 1-5 rating count."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    pairs = []
    for r in rows:
        mine = (r.get("my_rating") or "").strip()
        if mine in {"1", "2", "3", "4", "5"} and r.get("judge_avg"):
            pairs.append({"draft_id": r["draft_id"], "company": r["company"], "variant": r["variant"],
                          "judge": float(r["judge_avg"]), "mine": int(mine)})
    if not pairs:
        return {"rated": 0, "total": len(rows), "pairs": []}
    diffs = [abs(p["judge"] - p["mine"]) for p in pairs]
    return {
        "rated": len(pairs), "total": len(rows), "pairs": pairs,
        "judge_mean": sum(p["judge"] for p in pairs) / len(pairs),
        "my_mean": sum(p["mine"] for p in pairs) / len(pairs),
        "mean_abs_diff": sum(diffs) / len(diffs),
        "within_1": sum(1 for d in diffs if d <= 1),
        "correlation": pearson([p["judge"] for p in pairs], [p["mine"] for p in pairs]),
    }
