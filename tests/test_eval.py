"""Eval metrics on hand-computed examples, plus the label CSV round trip."""

import asyncio
import math

import pytest

from sort_sherr.matching import evaluate
from sort_sherr.matching.models import Preferences
from sort_sherr.matching.run import run_match
from sort_sherr.storage import db
from test_rerank import FakeLLM  # pytest puts tests/ on the import path


def test_precision_counts_only_good():
    labels = {1: 2, 2: 1, 3: 0, 4: 2}
    assert evaluate.precision_at_k([1, 2, 3, 4], labels, 4) == 0.5
    assert evaluate.precision_at_k([1, 9], labels, 2) == 0.5  # 9 unlabeled -> not good


def test_ndcg_by_hand():
    labels = {1: 2, 2: 1, 3: 0}
    assert evaluate.ndcg_at_k([1, 2, 3], labels, 3) == pytest.approx(1.0)  # perfect order
    # worst order: 3, 2, 1 -> DCG = 0 + 1/log2(3) + 2/log2(4); IDCG = 2 + 1/log2(3)
    dcg = 1 / math.log2(3) + 2 / 2
    idcg = 2 + 1 / math.log2(3)
    assert evaluate.ndcg_at_k([3, 2, 1], labels, 3) == pytest.approx(dcg / idcg)
    assert evaluate.ndcg_at_k([1, 2], {}, 2) == 0.0


def test_load_labels_accepts_words_and_numbers(tmp_path):
    f = tmp_path / "labels.csv"
    f.write_text("company_id,company_name,label\n1,A,good\n2,B,OK\n3,C,0\n4,D,\nx,E,good\n")
    assert evaluate.load_labels(f) == {1: 2, 2: 1, 3: 0}  # blank and bad ids skipped


def test_template_and_evaluate_on_a_run(match_db, fake_embedder, profile, tmp_path):
    cid = db.save_candidate(match_db, "h", profile.model_dump_json(), "{}", "fake")
    run_id = asyncio.run(run_match(match_db, fake_embedder, FakeLLM(), cid, Preferences(), progress=lambda s: None))

    path = tmp_path / "labels" / "me.csv"
    n = evaluate.write_template(match_db, run_id, path, k=3)
    rows = path.read_text().splitlines()
    assert rows[0] == "company_id,company_name,label" and len(rows) == n + 1 and 3 <= n <= 6

    vf = next(m["company_id"] for m in db.run_matches(match_db, run_id) if m["company_name"] == "VectorForge")
    path.write_text(path.read_text().replace(f"{vf},VectorForge,", f"{vf},VectorForge,good"))
    evaluate.write_template(match_db, run_id, path, k=3)  # re-writing keeps existing labels
    assert f"{vf},VectorForge,good" in path.read_text()

    results = evaluate.evaluate(match_db, run_id, evaluate.load_labels(path), k=3)
    assert set(results) == {"retrieval only", "retrieval + rerank"}
    for m in results.values():
        assert 0 <= m["precision"] <= 1 and 0 <= m["ndcg"] <= 1 and m["unlabeled"] >= 2
