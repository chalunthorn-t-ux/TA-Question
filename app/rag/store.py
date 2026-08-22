"""Vector store แบบไฟล์เดียว: numpy .npy สำหรับเวกเตอร์ + JSON สำหรับ metadata

ค้นแบบ hybrid = cosine similarity (semantic) ผสม BM25 บน character 3-gram (keyword)
n-gram ระดับตัวอักษรทำให้ใช้กับภาษาไทยได้โดยไม่ต้องตัดคำ
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import config
from .chunker import Chunk

_VECTORS_FILE = "vectors.npy"
_META_FILE = "index.json"

_K1 = 1.5
_B = 0.75


@dataclass
class SearchHit:
    chunk: dict
    score: float
    semantic: float
    keyword: float


def _ngrams(text: str, n: int = 3) -> list[str]:
    text = " ".join(text.lower().split())
    if len(text) < n:
        return [text] if text else []
    return [text[i : i + n] for i in range(len(text) - n + 1)]


class VectorStore:
    def __init__(self) -> None:
        self.vectors: np.ndarray = np.zeros((0, config.GEMINI_EMBED_DIM), dtype=np.float32)
        self.chunks: list[dict] = []
        self.backend: str = "none"
        self.built_at: str = ""
        # โครงสร้างสำหรับ BM25
        self._tf: list[Counter] = []
        self._df: Counter = Counter()
        self._lengths: np.ndarray = np.zeros(0, dtype=np.float32)
        self._avg_len: float = 1.0

    # ------------------------------------------------------------------ #
    @property
    def is_empty(self) -> bool:
        return not self.chunks

    def sources(self) -> list[dict]:
        counts = Counter(c["source"] for c in self.chunks)
        return [{"name": name, "chunks": n} for name, n in sorted(counts.items())]

    # ------------------------------------------------------------------ #
    def build(
        self,
        chunks: list[Chunk],
        vectors: np.ndarray,
        backend: str,
        built_at: str,
    ) -> None:
        self.chunks = [c.to_dict() for c in chunks]
        self.vectors = vectors.astype(np.float32)
        self.backend = backend
        self.built_at = built_at
        self._build_lexical()

    def _build_lexical(self) -> None:
        self._tf = []
        self._df = Counter()
        lengths: list[int] = []
        for chunk in self.chunks:
            grams = _ngrams(chunk["text"])
            tf = Counter(grams)
            self._tf.append(tf)
            lengths.append(len(grams))
            self._df.update(tf.keys())
        self._lengths = np.asarray(lengths, dtype=np.float32)
        self._avg_len = float(self._lengths.mean()) if len(self._lengths) else 1.0

    # ------------------------------------------------------------------ #
    def save(self, directory: Path | None = None) -> None:
        directory = directory or config.INDEX_DIR
        directory.mkdir(parents=True, exist_ok=True)
        np.save(directory / _VECTORS_FILE, self.vectors)
        (directory / _META_FILE).write_text(
            json.dumps(
                {
                    "backend": self.backend,
                    "built_at": self.built_at,
                    "embed_model": config.GEMINI_EMBED_MODEL,
                    "dim": int(self.vectors.shape[1]) if self.vectors.size else 0,
                    "chunks": self.chunks,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def load(self, directory: Path | None = None) -> bool:
        directory = directory or config.INDEX_DIR
        meta_path = directory / _META_FILE
        vec_path = directory / _VECTORS_FILE
        if not (meta_path.exists() and vec_path.exists()):
            return False

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.chunks = meta.get("chunks", [])
        self.backend = meta.get("backend", "unknown")
        self.built_at = meta.get("built_at", "")
        self.vectors = np.load(vec_path).astype(np.float32)

        if len(self.chunks) != self.vectors.shape[0]:
            raise ValueError("index เสียหาย: จำนวน chunk ไม่ตรงกับจำนวนเวกเตอร์")

        self._build_lexical()
        return True

    # ------------------------------------------------------------------ #
    def _bm25(self, query: str) -> np.ndarray:
        n_docs = len(self.chunks)
        scores = np.zeros(n_docs, dtype=np.float32)
        if n_docs == 0:
            return scores

        for gram in set(_ngrams(query)):
            df = self._df.get(gram, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            for i, tf_map in enumerate(self._tf):
                tf = tf_map.get(gram, 0)
                if tf == 0:
                    continue
                denom = tf + _K1 * (1 - _B + _B * self._lengths[i] / self._avg_len)
                scores[i] += idf * (tf * (_K1 + 1)) / denom

        peak = float(scores.max()) if scores.size else 0.0
        return scores / peak if peak > 0 else scores

    def search(
        self,
        query: str,
        query_vector: np.ndarray,
        top_k: int = 5,
        alpha: float | None = None,
    ) -> list[SearchHit]:
        if self.is_empty:
            return []

        alpha = config.HYBRID_ALPHA if alpha is None else alpha

        semantic = self.vectors @ query_vector.astype(np.float32)
        semantic = (semantic + 1.0) / 2.0          # cosine [-1,1] -> [0,1]
        keyword = self._bm25(query)
        combined = alpha * semantic + (1 - alpha) * keyword

        order = np.argsort(-combined)[: max(top_k, 1)]
        return [
            SearchHit(
                chunk=self.chunks[i],
                score=round(float(combined[i]), 4),
                semantic=round(float(semantic[i]), 4),
                keyword=round(float(keyword[i]), 4),
            )
            for i in order
        ]
