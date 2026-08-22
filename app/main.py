"""FastAPI application — เสิร์ฟหน้าเว็บ + API สำหรับถาม-ตอบและจัดการ index"""

from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import config
from .rag import pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ta-assistant")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if pipeline.load_index():
        store = pipeline.get_store()
        log.info("โหลด index แล้ว: %d chunk (backend=%s)", len(store.chunks), store.backend)
    else:
        log.info("ยังไม่มี index — วางไฟล์ใน data/ แล้วเรียก POST /api/ingest")
    if not config.has_api_key():
        log.warning("ยังไม่ได้ตั้ง GEMINI_API_KEY — ระบบจะทำงานในโหมดค้นหาเท่านั้น")
    yield


app = FastAPI(title=config.APP_TITLE, version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(config.TEMPLATE_DIR))


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1, le=20)


# --------------------------------------------------------------------------- #
# หน้าเว็บ
# --------------------------------------------------------------------------- #
@app.get("/")
async def index(request: Request):
    store = pipeline.get_store()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "title": config.APP_TITLE,
            "subtitle": config.APP_SUBTITLE,
            "has_api_key": config.has_api_key(),
            "chat_model": config.GEMINI_CHAT_MODEL,
            "chunk_count": len(store.chunks),
            "sources": store.sources(),
            "built_at": store.built_at,
        },
    )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/status")
async def status():
    store = pipeline.get_store()
    return {
        "has_api_key": config.has_api_key(),
        "chat_model": config.GEMINI_CHAT_MODEL,
        "embed_model": config.GEMINI_EMBED_MODEL,
        "embed_backend": store.backend,
        "chunk_count": len(store.chunks),
        "sources": store.sources(),
        "built_at": store.built_at,
        "top_k": config.TOP_K,
    }


@app.post("/api/ask")
async def api_ask(payload: AskRequest):
    history = [t.model_dump() for t in payload.history]
    # งาน embed/generate เป็น blocking I/O -> โยนเข้า threadpool กัน event loop ค้าง
    result = await run_in_threadpool(
        pipeline.ask, payload.question, history, payload.top_k
    )
    return result


@app.post("/api/ingest")
async def api_ingest():
    result = await run_in_threadpool(pipeline.ingest)
    if not result["ok"]:
        return JSONResponse(result, status_code=422)
    return result


@app.post("/api/upload")
async def api_upload(files: list[UploadFile] = File(...)):
    """อัปโหลดเอกสารเข้าโฟลเดอร์ data/ (ยังไม่สร้าง index — กด 'สร้าง Index' ต่อ)"""
    saved: list[str] = []
    rejected: list[dict] = []

    for upload in files:
        name = (upload.filename or "").strip()
        if not name:
            continue
        safe_name = name.replace("\\", "/").split("/")[-1]      # กัน path traversal
        suffix = ("." + safe_name.rsplit(".", 1)[-1]).lower() if "." in safe_name else ""

        if suffix not in config.SUPPORTED_EXTENSIONS:
            rejected.append({"name": safe_name, "reason": f"ไม่รองรับนามสกุล {suffix or '(ไม่มี)'}"})
            continue

        target = config.DATA_DIR / safe_name
        try:
            with target.open("wb") as fh:
                shutil.copyfileobj(upload.file, fh)
            saved.append(safe_name)
        except OSError as exc:
            rejected.append({"name": safe_name, "reason": str(exc)})
        finally:
            await upload.close()

    if not saved and rejected:
        raise HTTPException(status_code=422, detail={"saved": saved, "rejected": rejected})

    return {
        "ok": True,
        "saved": saved,
        "rejected": rejected,
        "message": f"อัปโหลด {len(saved)} ไฟล์แล้ว — กด “สร้าง Index” เพื่อให้ระบบเรียนรู้",
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
