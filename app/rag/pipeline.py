"""ประกอบทุกชิ้นเข้าด้วยกัน: ingest (สร้าง index) และ ask (ตอบคำถาม)"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from .. import config
from . import embedder, llm, redact
from .chunker import chunk_sections
from .loaders import RawSection, iter_documents, load_file
from .store import VectorStore

log = logging.getLogger(__name__)

_store = VectorStore()


def get_store() -> VectorStore:
    return _store


def load_index() -> bool:
    """โหลด index ที่สร้างไว้แล้วจากดิสก์ (เรียกตอนสตาร์ทเซิร์ฟเวอร์)"""
    try:
        return _store.load()
    except Exception as exc:  # noqa: BLE001
        log.error("โหลด index ไม่สำเร็จ: %s", exc)
        return False


# --------------------------------------------------------------------------- #
def ingest(data_dir: Path | None = None) -> dict:
    """อ่านเอกสารทั้งหมดใน data/ -> ตัด chunk -> embed -> เซฟ index"""
    data_dir = data_dir or config.DATA_DIR

    sections: list[RawSection] = []
    files_ok: list[dict] = []
    files_failed: list[dict] = []

    for path in iter_documents(data_dir):
        try:
            found = load_file(path)
            sections.extend(found)
            files_ok.append({"name": path.name, "sections": len(found)})
            log.info("อ่าน %s -> %d ส่วน", path.name, len(found))
        except Exception as exc:  # noqa: BLE001 — ไฟล์เดียวพังไม่ควรล้มทั้ง batch
            files_failed.append({"name": path.name, "error": str(exc)})
            log.warning("อ่าน %s ไม่ได้: %s", path.name, exc)

    # ลบชื่อบุคคลก่อนตัด chunk — ทำที่นี่ทีเดียว ไฟล์ใหม่ที่เพิ่มมาภายหลังจะถูกปกป้องเอง
    redacted = redact.redact_sections(sections)

    if not sections:
        return {
            "ok": False,
            "message": (
                f"ไม่พบเนื้อหาที่อ่านได้ในโฟลเดอร์ {data_dir} "
                f"(รองรับ: {', '.join(sorted(config.SUPPORTED_EXTENSIONS))})"
            ),
            "files": files_ok,
            "failed": files_failed,
            "chunks": 0,
        }

    chunks = chunk_sections(sections, config.CHUNK_SIZE, config.CHUNK_OVERLAP)
    vectors, backend = embedder.embed_documents([c.text for c in chunks])

    _store.build(chunks, vectors, backend, datetime.now().isoformat(timespec="seconds"))
    _store.save()

    return {
        "ok": True,
        "message": f"สร้าง index สำเร็จ {len(chunks)} chunk จาก {len(files_ok)} ไฟล์",
        "files": files_ok,
        "failed": files_failed,
        "chunks": len(chunks),
        "backend": backend,
        "built_at": _store.built_at,
        "redacted_names": redacted,
    }


# --------------------------------------------------------------------------- #
def ask(question: str, history: list[dict] | None = None, top_k: int | None = None) -> dict:
    # จับเวลาตั้งแต่ต้น เพื่อไม่ให้ใช้เกินเพดานของ serverless แล้วถูกตัดทิ้ง (504)
    deadline = time.monotonic() + config.REQUEST_BUDGET_SECONDS

    question = (question or "").strip()
    if not question:
        return {"answer": "กรุณาพิมพ์คำถามก่อนนะครับ", "sources": [], "status": "empty_question"}

    if _store.is_empty:
        return {
            "answer": (
                "ยังไม่มีข้อมูลในระบบเลยครับ — วางไฟล์เอกสารไว้ในโฟลเดอร์ `data/` "
                "แล้วกดปุ่ม **สร้าง Index** ที่แถบด้านซ้ายก่อนนะครับ"
            ),
            "sources": [],
            "status": "no_index",
        }

    query_vec = embedder.embed_query(question, _store.backend)
    hits = _store.search(question, query_vec, top_k or config.TOP_K)
    relevant = [h for h in hits if h.score >= config.MIN_SCORE] or hits[:1]

    sources = [
        {
            "ref": n,
            "source": h.chunk["source"],
            "locator": h.chunk.get("locator", ""),
            "score": h.score,
            "semantic": h.semantic,
            "keyword": h.keyword,
            "excerpt": h.chunk["text"][:600],
        }
        for n, h in enumerate(relevant, start=1)
    ]

    try:
        answer = llm.generate_answer(question, relevant, history, deadline)
        status = "ok"
    except llm.LLMError as exc:
        # สรุปคำตอบไม่ได้ (โควตาหมด / Gemini ล่ม / ยังไม่มี key)
        # แสดงเฉพาะข้อความภาษาคน — รายละเอียดทางเทคนิคไปอยู่ใน log
        log.warning("สร้างคำตอบไม่ได้: %s", exc.detail)
        answer = exc.friendly
        if config.SHOW_SOURCES:
            # โหมดตรวจสอบ: คืนข้อความจากเอกสารให้อ่านเอง ดีกว่าปล่อยมือเปล่า
            preview = "\n\n".join(
                f"**[{s['ref']}] {s['source']}"
                + (f" · {s['locator']}" if s["locator"] else "")
                + f"**\n{s['excerpt']}"
                for s in sources
            )
            answer += f"\n\nระหว่างนี้ ข้อความจากเอกสารที่ตรงกับคำถามที่สุดคือ\n\n{preview}"
        status = "retrieval_only"

    # ไม่ส่ง sources ออกไปเลยเมื่อปิดการแสดง — กันเนื้อหาเอกสารรั่วผ่าน API
    # แม้หน้าเว็บจะไม่แสดง คนที่เปิด DevTools ดูก็ยังเห็นได้ถ้าส่งไป
    return {
        "answer": answer,
        "sources": sources if config.SHOW_SOURCES else [],
        "status": status,
    }
