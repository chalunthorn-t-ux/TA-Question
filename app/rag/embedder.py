"""สร้าง embedding ด้วย Gemini API (มี fallback แบบ local ถ้ายังไม่ใส่ API key)

Fallback ใช้ hashing ของ character n-gram — ไม่ฉลาดเท่าโมเดลจริง แต่ทำให้
ระบบเดินได้ทันทีตอนดู template และตัวเลข dimension ยังเข้ากันได้
"""

from __future__ import annotations

import hashlib
import logging

import httpx
import numpy as np

from .. import config

log = logging.getLogger(__name__)

_BATCH_SIZE = 32
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class EmbeddingError(RuntimeError):
    pass


def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


# --------------------------------------------------------------------------- #
# Fallback: hashing vectorizer (ไม่ต้องต่อเน็ต)
# --------------------------------------------------------------------------- #
def _char_ngrams(text: str, n: int = 3) -> list[str]:
    text = " ".join(text.lower().split())
    if len(text) < n:
        return [text] if text else []
    return [text[i : i + n] for i in range(len(text) - n + 1)]


def _hash_embed(texts: list[str], dim: int) -> np.ndarray:
    mat = np.zeros((len(texts), dim), dtype=np.float32)
    for row, text in enumerate(texts):
        for gram in _char_ngrams(text):
            digest = hashlib.md5(gram.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "little") % dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            mat[row, idx] += sign
    return _normalize(mat)


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #
def _gemini_embed(texts: list[str], task_type: str) -> np.ndarray:
    url = (
        f"{config.GEMINI_BASE_URL}/models/{config.GEMINI_EMBED_MODEL}"
        ":batchEmbedContents"
    )
    headers = {
        "x-goog-api-key": config.GEMINI_API_KEY,
        "Content-Type": "application/json",
    }

    vectors: list[list[float]] = []
    with httpx.Client(timeout=_TIMEOUT) as client:
        for start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[start : start + _BATCH_SIZE]
            payload = {
                "requests": [
                    {
                        "model": f"models/{config.GEMINI_EMBED_MODEL}",
                        "content": {"parts": [{"text": t[:8000]}]},
                        "taskType": task_type,
                        "outputDimensionality": config.GEMINI_EMBED_DIM,
                    }
                    for t in batch
                ]
            }
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise EmbeddingError(
                    f"Gemini embedding ล้มเหลว ({resp.status_code}): {resp.text[:300]}"
                )
            data = resp.json()
            for item in data.get("embeddings", []):
                vectors.append(item.get("values", []))

    if len(vectors) != len(texts):
        raise EmbeddingError(
            f"จำนวน embedding ที่ได้ ({len(vectors)}) ไม่ตรงกับข้อความที่ส่ง ({len(texts)})"
        )
    return _normalize(np.asarray(vectors, dtype=np.float32))


# --------------------------------------------------------------------------- #
# API สาธารณะ
# --------------------------------------------------------------------------- #
def embed_documents(texts: list[str]) -> tuple[np.ndarray, str]:
    """คืน (เมทริกซ์ embedding, ชื่อ backend ที่ใช้จริง)"""
    if not texts:
        return np.zeros((0, config.GEMINI_EMBED_DIM), dtype=np.float32), "none"

    if config.has_api_key():
        try:
            return _gemini_embed(texts, "RETRIEVAL_DOCUMENT"), config.GEMINI_EMBED_MODEL
        except Exception as exc:  # noqa: BLE001 — ล้มแล้วยังต้องใช้งานต่อได้
            log.warning("ใช้ Gemini embedding ไม่ได้ (%s) — สลับไปใช้ hashing ชั่วคราว", exc)

    return _hash_embed(texts, config.GEMINI_EMBED_DIM), "hashing-fallback"


def embed_query(text: str, backend: str = "") -> np.ndarray:
    """embed คำถาม — ต้องใช้ backend เดียวกับที่สร้าง index ไว้ ไม่งั้นคะแนนเทียบกันไม่ได้"""
    if backend == "hashing-fallback" or not config.has_api_key():
        return _hash_embed([text], config.GEMINI_EMBED_DIM)[0]
    try:
        return _gemini_embed([text], "RETRIEVAL_QUERY")[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("embed คำถามด้วย Gemini ไม่ได้ (%s) — ใช้ hashing", exc)
        return _hash_embed([text], config.GEMINI_EMBED_DIM)[0]
