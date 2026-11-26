"""Embeddings: turn text into a vector so "similar meaning" becomes "small angle between vectors".

- `Embedder` is the interface (any class with `name` and `embed()`), so the model can be swapped.
- Default: a small free model that runs on your own computer (BAAI/bge-small-en-v1.5, ~130 MB).
  Nothing is sent anywhere, so it's fine for resume text too.
- `embed_cached()` stores every vector in SQLite keyed by sha256(text). Re-running `match` only
  embeds companies whose document changed (e.g. new jobs), so it takes seconds, not minutes.
"""

import hashlib
from typing import Protocol

import numpy as np

from .. import match_config
from ..storage import db


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        """One row per text, each row of length 1 (normalized), so a dot product = cosine similarity."""
        ...


class SentenceTransformerEmbedder:
    # bge models were trained with this prefix on the "question" side (our candidate),
    # and no prefix on the "document" side (companies).
    QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, name: str = match_config.EMBED_MODEL):
        self.name = name
        self._model = None  # loaded on first use: importing torch takes a few seconds

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.name)
        if is_query and "bge" in self.name.lower():
            texts = [self.QUERY_PREFIX + t for t in texts]
        vectors = self._model.encode(texts, normalize_embeddings=True, batch_size=64, show_progress_bar=len(texts) > 200)
        return np.asarray(vectors, dtype=np.float32)


def embed_cached(conn, embedder: Embedder, texts: list[str], is_query: bool = False) -> tuple[np.ndarray, int]:
    """Like embedder.embed(), but reuses vectors saved in the DB.
    Returns (matrix with one row per text, how many had to be newly embedded)."""
    hashes = [hashlib.sha256(f"{int(is_query)}\n{t}".encode()).hexdigest() for t in texts]
    saved = db.get_embeddings(conn, list(set(hashes)), embedder.name)
    missing = sorted({h for h in hashes if h not in saved})
    if missing:
        text_of = dict(zip(hashes, texts))
        new_vectors = embedder.embed([text_of[h] for h in missing], is_query=is_query)
        rows = [(h, v.astype(np.float32).tobytes()) for h, v in zip(missing, new_vectors)]
        db.put_embeddings(conn, rows, embedder.name)
        saved.update(dict(rows))
    matrix = np.vstack([np.frombuffer(saved[h], dtype=np.float32) for h in hashes]) if hashes else np.zeros((0, 0))
    return matrix, len(missing)
