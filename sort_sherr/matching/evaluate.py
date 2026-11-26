"""Does the LLM rerank earn its cost? Compare two orderings of the SAME run against your own labels:

  (a) retrieval only: companies sorted by embedding similarity
  (b) retrieval + rerank: companies sorted by final_score

Labels: a CSV you fill in by hand, one company per row:  company_id,company_name,label
  label = good / ok / bad  (or 2 / 1 / 0)

Metrics, both over the top K (default 15):
  - precision@K: what share of the top K you labeled "good"
  - nDCG@K: like precision, but rewards putting the BEST companies HIGHEST
    (good=2, ok=1, bad=0 points, divided by log2(position+1); 1.0 = the perfect order of your labels)
Companies in the top K you didn't label count as 0 and are reported as "unlabeled", so you can
label them and run again. `eval --make-template` writes the CSV for you with exactly the companies
that appear in either top K.
"""

import csv
import math
from pathlib import Path

from ..storage import db

GAINS = {"good": 2, "ok": 1, "bad": 0, "2": 2, "1": 1, "0": 0}


def load_labels(path: Path) -> dict[int, int]:
    labels = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            label = (row.get("label") or "").strip().lower()
            if label in GAINS and (row.get("company_id") or "").strip().isdigit():
                labels[int(row["company_id"])] = GAINS[label]
    return labels


def precision_at_k(ranking: list[int], labels: dict[int, int], k: int) -> float:
    return sum(1 for cid in ranking[:k] if labels.get(cid) == 2) / k


def ndcg_at_k(ranking: list[int], labels: dict[int, int], k: int) -> float:
    dcg = sum(labels.get(cid, 0) / math.log2(i + 2) for i, cid in enumerate(ranking[:k]))
    ideal = sorted(labels.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def orderings(conn, run_id: int) -> dict[str, list[int]]:
    return {
        "retrieval only": [m["company_id"] for m in db.run_matches(conn, run_id, "retrieval")],
        "retrieval + rerank": [m["company_id"] for m in db.run_matches(conn, run_id, "final")],
    }


def evaluate(conn, run_id: int, labels: dict[int, int], k: int = 15) -> dict[str, dict]:
    out = {}
    for name, ranking in orderings(conn, run_id).items():
        out[name] = {
            "precision": precision_at_k(ranking, labels, k),
            "ndcg": ndcg_at_k(ranking, labels, k),
            "unlabeled": sum(1 for cid in ranking[:k] if cid not in labels),
        }
    return out


def write_template(conn, run_id: int, path: Path, k: int = 15) -> int:
    """CSV with every company in either top K (about 20-30), label column empty. Keeps labels
    already in the file, so you can re-run it after a new run without losing work."""
    old = load_labels(path) if path.exists() else {}
    names = {m["company_id"]: m["company_name"] for m in db.run_matches(conn, run_id)}
    ids: list[int] = []
    for ranking in orderings(conn, run_id).values():
        ids += [cid for cid in ranking[:k] if cid not in ids]
    ids += [cid for cid in old if cid not in ids]
    back = {2: "good", 1: "ok", 0: "bad"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company_id", "company_name", "label"])
        for cid in ids:
            w.writerow([cid, names.get(cid, ""), back.get(old.get(cid), "")])
    return len(ids)
