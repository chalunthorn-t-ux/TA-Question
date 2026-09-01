"""ประกอบทุกชิ้นเข้าด้วยกัน: ingest (สร้าง index) และ ask (ตอบคำถาม)"""

from __future__ import annotations

import logging
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .. import blobstore, config
from . import embedder, llm, redact
from .chunker import chunk_sections
from .loaders import RawSection, iter_documents, load_file
from .store import VectorStore

log = logging.getLogger(__name__)

_store = VectorStore()

# ไฟล์ที่ประกอบกันเป็น index หนึ่งชุด — ต้องไปด้วยกันเสมอ
INDEX_FILES = ("index.json", "vectors.npy")

# บอกว่า index ที่โหลดอยู่มาจากไหน ("blob" / "disk" / "none") ใช้รายงานใน /healthz
index_source: str = "none"


def get_store() -> VectorStore:
    return _store


def _runtime_dir() -> Path:
    """โฟลเดอร์ชั่วคราวสำหรับวางไฟล์ที่ดึงมาจาก Blob

    บน Vercel เขียนได้เฉพาะ /tmp — tempfile.gettempdir() ชี้ไปที่นั่นให้เอง
    """
    return Path(tempfile.gettempdir()) / "ta-index"


def _fetch_index_from_blob() -> Path | None:
    """ดึง index ล่าสุดจาก Blob ลงโฟลเดอร์ชั่วคราว คืน None ถ้ายังไม่เคย push"""
    target = _runtime_dir()
    target.mkdir(parents=True, exist_ok=True)

    for name in INDEX_FILES:
        data = blobstore.get(name)
        if data is None:
            log.warning("ยังไม่มี %s บน Blob — ข้ามไปใช้ index ในดิสก์แทน", name)
            return None
        (target / name).write_bytes(data)

    return target


def load_index() -> bool:
    """โหลด index ที่สร้างไว้แล้ว (เรียกตอนสตาร์ทเซิร์ฟเวอร์)

    ถ้าตั้ง BLOB_READ_WRITE_TOKEN ไว้ จะเอาของบน Blob ก่อนเสมอ
    เพราะเป็นที่เดียวที่อัปเดตได้โดยไม่ต้อง deploy ใหม่
    """
    global index_source

    if blobstore.enabled():
        try:
            directory = _fetch_index_from_blob()
            if directory is not None and _store.load(directory):
                index_source = "blob"
                return True
        except Exception as exc:  # noqa: BLE001
            # ดึงจาก Blob ไม่ได้ ไม่ควรทำให้แอปไม่ขึ้น — ถอยไปใช้ของในดิสก์
            log.critical("ดึง index จาก Blob ไม่สำเร็จ: %s", exc)

    try:
        if _store.load():
            index_source = "disk"
            return True
    except Exception as exc:  # noqa: BLE001
        log.error("โหลด index ไม่สำเร็จ: %s", exc)

    index_source = "none"
    return False


def push_index(directory: Path | None = None) -> list[dict]:
    """อัป index จากดิสก์ขึ้น Blob — ใช้หลัง ingest ในเครื่องเสร็จ"""
    if not blobstore.enabled():
        raise RuntimeError(
            "ยังไม่ได้ตั้ง BLOB_READ_WRITE_TOKEN — สร้าง Blob store ที่ Vercel "
            "แล้วก็อปโทเคนมาใส่ .env ก่อนนะคะ"
        )

    directory = directory or config.INDEX_DIR
    uploaded: list[dict] = []

    for name in INDEX_FILES:
        path = directory / name
        if not path.exists():
            raise FileNotFoundError(f"ไม่พบ {path} — รัน ingest ให้เสร็จก่อนแล้วค่อย push")
        data = path.read_bytes()
        content_type = "application/json" if name.endswith(".json") else "application/octet-stream"
        url = blobstore.put(name, data, content_type)
        uploaded.append({"name": name, "bytes": len(data), "url": url})

    return uploaded


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
        return {
            "answer": "กรุณาพิมพ์คำถามก่อนนะคะ",
            "sources": [],
            "status": "empty_question",
            "top_score": 0.0,
        }

    if _store.is_empty:
        return {
            "answer": (
                "ยังไม่มีข้อมูลในระบบเลยค่ะ — วางไฟล์เอกสารไว้ในโฟลเดอร์ `data/` "
                "แล้วกดปุ่ม **สร้าง Index** ที่แถบด้านซ้ายก่อนนะคะ"
            ),
            "sources": [],
            "status": "no_index",
            "top_score": 0.0,
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
        # คะแนนสูงสุดที่ค้นได้ — ใช้ดูว่า "ตอบได้" แต่ข้อมูลอ่อนหรือเปล่า
        "top_score": round(float(relevant[0].score), 4) if relevant else 0.0,
    }
