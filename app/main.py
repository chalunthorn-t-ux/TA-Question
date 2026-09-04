"""FastAPI application — เสิร์ฟหน้าเว็บ + API สำหรับถาม-ตอบและจัดการ index

ทุกหน้าและทุก API ต้องเข้าสู่ระบบก่อน ยกเว้นหน้า login/register และ /healthz
งานที่แก้ข้อมูล (อัปโหลดเอกสาร สร้าง index) จำกัดเฉพาะ admin
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from functools import partial
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from . import auth, config, db, docstore, gaps, objectstore, settings_repo
from .rag import pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ta-assistant")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # สร้างตารางที่ยังไม่มีให้เอง — เรียกซ้ำได้ ไม่พัง
    # ถ้าไม่ทำ ผู้ใช้ต้องรู้เองว่าต้องรัน scripts/users.py init-db ก่อน
    # ซึ่งอาการเวลาลืมคือ "ล็อกอินไม่ติด/ไม่มีความรู้" ที่ไล่หาสาเหตุยากมาก
    if db.enabled():
        try:
            await run_in_threadpool(db.init_schema)
        except Exception as exc:  # noqa: BLE001 — ต่อฐานข้อมูลไม่ได้ ไม่ควรทำให้แอปไม่ขึ้น
            log.critical("เตรียมตารางในฐานข้อมูลไม่สำเร็จ: %s", exc)

    if pipeline.load_index():
        store = pipeline.get_store()
        log.info(
            "โหลด index แล้ว: %d chunk (backend=%s, มาจาก=%s)",
            len(store.chunks), store.backend, pipeline.index_source,
        )
    else:
        log.info("ยังไม่มี index — วางไฟล์ใน data/ แล้วเรียก POST /api/ingest")

    log.info("ที่เก็บบัญชีผู้ใช้: %s", auth.storage_backend())
    if config.READ_ONLY_FS and not db.enabled():
        log.critical(
            "เซิร์ฟเวอร์เขียนไฟล์ไม่ได้ และยังไม่ได้ตั้ง DATABASE_URL "
            "-> สมัครสมาชิก/แก้บัญชีจะทำไม่ได้เลย ให้ผูก Postgres (เช่น Neon) ที่ผู้ให้บริการก่อน"
        )

    if not config.has_api_key():
        log.warning("ยังไม่ได้ตั้ง GEMINI_API_KEY — ระบบจะทำงานในโหมดค้นหาเท่านั้น")

    if not auth.has_any_user():
        log.warning("ยังไม่มีบัญชีผู้ใช้ — คนแรกที่สมัครที่ /register จะได้สิทธิ์ admin")
    else:
        log.info("มีบัญชีผู้ใช้ %d คน", auth.user_count())
    yield


# ปิดหน้า /docs /redoc /openapi.json ตอน production
# เพราะเปิดดูได้โดยไม่ต้องล็อกอิน = เผยโครง API ให้คนนอกเห็น
# เปิดกลับได้ด้วย ENABLE_DOCS=1 เวลาพัฒนา
app = FastAPI(
    title=config.APP_TITLE,
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs" if config.ENABLE_DOCS else None,
    redoc_url="/redoc" if config.ENABLE_DOCS else None,
    openapi_url="/openapi.json" if config.ENABLE_DOCS else None,
)

# คุกกี้ session — https_only ปิดไว้เพราะรันในเครื่องเป็น http
# ถ้า deploy ขึ้น Vercel (https) ให้ตั้ง SESSION_HTTPS_ONLY=1 ใน environment
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.session_secret(),
    session_cookie="ta_session",
    max_age=config.SESSION_MAX_AGE,
    same_site="lax",
    https_only=config.SESSION_HTTPS_ONLY,
)

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
# ตัวช่วยเรื่องสิทธิ์
# --------------------------------------------------------------------------- #
def current_user(request: Request) -> auth.User | None:
    data = request.session.get("user")
    if not isinstance(data, dict):
        return None
    username = data.get("username")
    if not username:
        return None
    return auth.User(username=username, role=data.get("role", auth.ROLE_MEMBER))


def require_user(request: Request) -> auth.User:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="กรุณาเข้าสู่ระบบก่อนใช้งาน")
    return user


def require_admin(request: Request) -> auth.User:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="เฉพาะผู้ดูแลระบบเท่านั้นที่ทำรายการนี้ได้")
    return user


# --------------------------------------------------------------------------- #
# จำกัดจำนวนคำถามต่อ IP
#
# หน้าถาม-ตอบเปิดให้ทุกคนที่มีลิงก์ ทุกคำถามใช้โควตา Gemini ของเจ้าของระบบ
# ถ้าไม่จำกัด ลิงก์หลุดในกลุ่มแชทครั้งเดียวก็อาจโควตาหมดในไม่กี่นาที
#
# เก็บในหน่วยความจำ: บน serverless จะนับแยกตาม instance จึงกันได้ไม่สมบูรณ์
# แต่ยังช่วยตัดการยิงรัว ๆ จากที่เดียวได้
# --------------------------------------------------------------------------- #
_ask_history: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # Vercel และ proxy อื่นส่ง IP จริงมาใน x-forwarded-for (ตัวแรกสุด)
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_check(ip: str) -> int:
    """คืนจำนวนวินาทีที่ต้องรอ ถ้ายังไม่เกินโควตาคืน 0"""
    if config.ASK_RATE_LIMIT <= 0:
        return 0

    now = time.monotonic()
    window = config.ASK_RATE_WINDOW
    recent = [t for t in _ask_history.get(ip, []) if now - t < window]

    if len(recent) >= config.ASK_RATE_LIMIT:
        _ask_history[ip] = recent
        return int(window - (now - recent[0])) + 1

    recent.append(now)
    _ask_history[ip] = recent

    # กันหน่วยความจำบวมถ้ามี IP เข้ามาเยอะ
    if len(_ask_history) > 5000:
        for key in [k for k, v in _ask_history.items() if not v or now - v[-1] > window]:
            _ask_history.pop(key, None)
    return 0


def _auth_page(
    request: Request,
    mode: str,
    error: str = "",
    username: str = "",
    notice: str = "",
):
    """หน้า login/register — mode คือ 'login' หรือ 'register'"""
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "title": config.APP_TITLE,
            "subtitle": config.APP_SUBTITLE,
            "mode": mode,
            "error": error,
            "username": username,
            # ยังไม่มีใครในระบบ -> ชวนให้สมัครเป็นผู้ดูแลคนแรก
            "first_user": not auth.has_any_user(),
            "notice": notice,
            "min_password": auth.MIN_PASSWORD,
        },
        status_code=200 if not error else 400,
    )


# --------------------------------------------------------------------------- #
# หน้า login / register
# --------------------------------------------------------------------------- #
@app.get("/login")
async def login_page(request: Request, closed: int = 0):
    if current_user(request):
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not auth.has_any_user():
        return RedirectResponse("/register", status_code=HTTP_303_SEE_OTHER)
    return _auth_page(request, "login", notice="closed" if closed else "")


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    try:
        user = await run_in_threadpool(auth.authenticate, username, password)
    except auth.AuthError as exc:
        log.info("เข้าสู่ระบบไม่สำเร็จ: %s", username[:32])
        return _auth_page(request, "login", str(exc), username)

    request.session.clear()      # กัน session fixation
    request.session["user"] = {"username": user.username, "role": user.role}
    log.info("เข้าสู่ระบบ: %s (%s)", user.username, user.role)
    return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)


@app.get("/register")
async def register_page(request: Request):
    """สมัครเองได้เฉพาะตอนระบบยังไม่มีใครเลย (ตั้งบัญชีผู้ดูแลคนแรก)

    หลังจากนั้นการสร้างบัญชีเป็นหน้าที่ของแอดมินที่ /admin/users
    ถ้าปิดตายตั้งแต่แรกจะไม่มีทางสร้างแอดมินคนแรกได้เลยตอนติดตั้งใหม่
    """
    if current_user(request):
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if auth.has_any_user():
        return RedirectResponse("/login?closed=1", status_code=HTTP_303_SEE_OTHER)
    return _auth_page(request, "register")


@app.post("/register")
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    # กันคนยิง POST ตรงมาสมัครเองหลังมีบัญชีแล้ว
    if auth.has_any_user():
        return RedirectResponse("/login?closed=1", status_code=HTTP_303_SEE_OTHER)

    try:
        user = await run_in_threadpool(auth.register, username, password, confirm)
    except auth.AuthError as exc:
        return _auth_page(request, "register", str(exc), username)

    request.session.clear()
    request.session["user"] = {"username": user.username, "role": user.role}
    return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# จัดการสมาชิก — แอดมินเท่านั้น
# --------------------------------------------------------------------------- #
def _flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def _take_flash(request: Request) -> dict | None:
    return request.session.pop("flash", None)


@app.get("/admin/settings")
async def settings_page(request: Request, user: auth.User = Depends(require_admin)):
    """ตั้งค่าที่จำเป็นจากหน้าเว็บ แทนการไปตั้ง env ที่ผู้ให้บริการ

    มีหน้านี้เพราะ env บน Vercel ที่ทำเป็น Sensitive แล้วอ่านกลับไม่ได้
    และแก้ทีต้อง redeploy — ทำให้ตั้งค่าผิดแล้วไล่หาสาเหตุยากมาก
    """
    store = pipeline.get_store()
    from_env = bool(os.getenv("SESSION_SECRET", "").strip())
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "title": config.APP_TITLE,
            "user": user,
            "flash": _take_flash(request),
            "db_ready": db.enabled(),
            "user_storage": auth.storage_backend(),
            "object_storage": objectstore.backend(),
            "has_session_secret": auth.has_session_secret(),
            "session_secret_from": "environment" if from_env else (
                "ฐานข้อมูล" if settings_repo.get(settings_repo.KEY_SESSION_SECRET) else "-"
            ),
            "has_api_key": config.has_api_key(),
            "api_key_from": "environment" if config.GEMINI_API_KEY else (
                "ฐานข้อมูล" if config.has_api_key() else "-"
            ),
            "chunk_count": len(store.chunks),
            "document_count": len(store.sources()),
            "index_source": pipeline.index_source,
        },
    )


@app.post("/admin/settings/api-key")
async def settings_api_key(
    request: Request,
    api_key: str = Form(...),
    user: auth.User = Depends(require_admin),
):
    key = api_key.strip()
    try:
        await run_in_threadpool(settings_repo.set, settings_repo.KEY_GEMINI_API_KEY, key)
    except Exception as exc:  # noqa: BLE001
        _flash(request, f"บันทึกคีย์ไม่สำเร็จ: {exc}", "error")
        return RedirectResponse("/admin/settings", status_code=HTTP_303_SEE_OTHER)

    # อย่าเขียนคีย์ลง log — บอกแค่ว่าใครเปลี่ยนและยาวเท่าไร
    log.info("อัปเดต Gemini API key (โดย %s, ยาว %d ตัวอักษร)", user.username, len(key))
    _flash(request, "บันทึกคีย์แล้ว ระบบพร้อมตอบคำถามทันที")
    return RedirectResponse("/admin/settings", status_code=HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# คลังความรู้ — จัดการไฟล์เอกสารต้นฉบับจากหน้าเว็บ
# --------------------------------------------------------------------------- #
def _size_text(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


@app.get("/admin/knowledge")
async def knowledge_page(request: Request, user: auth.User = Depends(require_admin)):
    """จัดการไฟล์ความรู้ — เพิ่ม แทนที่ ดาวน์โหลดไปแก้ และลบ

    หน้าถาม-ตอบอัปโหลดไฟล์ได้อย่างเดียว พอเอกสารมีแก้ไข (ซึ่งมีแน่)
    จึงไม่มีทางเอาของเก่าออก หรือเช็คว่าตอนนี้ในคลังมีไฟล์อะไรอยู่บ้าง
    """
    store = pipeline.get_store()
    indexed = {s["name"]: s["chunks"] for s in store.sources()}

    files: list[dict] = []
    try:
        raw = await run_in_threadpool(docstore.list_documents)
    except Exception as exc:  # noqa: BLE001 — ที่เก็บล่มไม่ควรทำให้หน้าเปิดไม่ขึ้น
        raw = []
        log.error("อ่านรายชื่อเอกสารไม่สำเร็จ: %s", exc)
        _flash(request, f"อ่านรายชื่อเอกสารจากที่เก็บไม่สำเร็จ: {exc}", "error")

    for item in raw:
        files.append(
            {
                "name": item["name"],
                "size_text": _size_text(item["size"]),
                "modified": (item.get("modified") or "")[:16].replace("T", " "),
                "chunks": indexed.pop(item["name"], 0),
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="knowledge.html",
        context={
            "title": config.APP_TITLE,
            "user": user,
            "flash": _take_flash(request),
            "files": files,
            # เอกสารที่ยังอยู่ใน index แต่ไม่มีไฟล์ต้นฉบับให้แก้แล้ว
            # (ลบไฟล์ตรงที่เก็บไปแล้ว หรือ index มาจากรอบที่ ingest ในเครื่อง)
            "orphans": [{"name": n, "chunks": c} for n, c in sorted(indexed.items())],
            "storage": docstore.backend(),
            "writable": docstore.writable(),
            "extensions": ", ".join(sorted(config.SUPPORTED_EXTENSIONS)),
            "chunk_count": len(store.chunks),
            "index_source": pipeline.index_source,
            "built_at": store.built_at,
        },
    )


@app.post("/admin/knowledge/upload")
async def knowledge_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    user: auth.User = Depends(require_admin),
):
    """เพิ่มหรือแทนที่เอกสาร — ชื่อซ้ำคือการอัปเดตทับ ซึ่งเป็นวิธีแก้ไขเอกสาร"""
    saved, replaced, rejected = [], [], []

    # ถามรายชื่อเดิมทีเดียว — บน Blob การเช็คทีละไฟล์คือ list ทั้งโฟลเดอร์ทุกครั้ง
    try:
        existing = await run_in_threadpool(docstore.names)
    except Exception as exc:  # noqa: BLE001 — แค่ทำให้บอกไม่ได้ว่า "ทับของเดิม" ไม่ใช่เรื่องคอขาดบาดตาย
        log.warning("อ่านรายชื่อเอกสารเดิมไม่สำเร็จ: %s", exc)
        existing = set()

    for upload in files:
        try:
            name = docstore.safe_name(upload.filename or "")
            docstore.check_supported(name)
            data = await run_in_threadpool(upload.file.read)
            existed = name in existing
            await run_in_threadpool(
                docstore.save, name, data,
                upload.content_type or "application/octet-stream",
            )
            (replaced if existed else saved).append(name)
        except (docstore.DocError, OSError, httpx.HTTPError) as exc:
            rejected.append(f"{upload.filename or '(ไม่มีชื่อ)'} ({exc})")
        finally:
            await upload.close()

    parts = []
    if saved:
        parts.append(f"เพิ่ม {len(saved)} ไฟล์")
    if replaced:
        parts.append(f"แทนที่ของเดิม {len(replaced)} ไฟล์ ({', '.join(replaced)})")

    if parts:
        log.info("อัปโหลดเอกสาร %s (โดย %s)", saved + replaced, user.username)
        _flash(
            request,
            " และ ".join(parts) + " แล้ว — กด “อัปเดต Index” เพื่อให้ระบบเรียนรู้ของใหม่"
            + (f" · ข้ามไป: {'; '.join(rejected)}" if rejected else ""),
        )
    else:
        _flash(request, f"ไม่ได้รับไฟล์ไหนเลย: {'; '.join(rejected) or 'ไม่มีไฟล์'}", "error")

    return RedirectResponse("/admin/knowledge", status_code=HTTP_303_SEE_OTHER)


@app.get("/admin/knowledge/download")
async def knowledge_download(name: str, user: auth.User = Depends(require_admin)):
    """ดาวน์โหลดไฟล์ต้นฉบับไปแก้ แล้วอัปกลับมาทับด้วยชื่อเดิม"""
    try:
        safe = docstore.safe_name(name)
    except docstore.DocError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    data = await run_in_threadpool(docstore.read, safe)
    if data is None:
        raise HTTPException(status_code=404, detail=f"ไม่พบไฟล์ “{safe}” ในคลังค่ะ")

    # ชื่อไฟล์ภาษาไทยใส่ใน header ตรง ๆ ไม่ได้ (header เป็น latin-1)
    # filename* แบบ RFC 5987 รองรับ UTF-8 และเบราว์เซอร์ปัจจุบันอ่านได้หมด
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "content-disposition": "attachment; filename*=UTF-8''" + quote(safe)
        },
    )


@app.post("/admin/knowledge/delete")
async def knowledge_delete(
    request: Request,
    name: str = Form(...),
    user: auth.User = Depends(require_admin),
):
    """ลบเอกสารออกจากคลัง — เอาออกจาก index ด้วยในคราวเดียว

    ลบแค่ไฟล์อย่างเดียวไม่พอ เพราะ index เก็บข้อความไว้ในตัวเองแล้ว
    ระบบจะยังตอบจากเอกสารที่ "ลบไปแล้ว" ต่อไปจนกว่าจะสร้าง index ใหม่ทั้งหมด
    """
    try:
        safe = docstore.safe_name(name)
        removed_file = await run_in_threadpool(docstore.delete, safe)
    except docstore.DocError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/admin/knowledge", status_code=HTTP_303_SEE_OTHER)
    except (OSError, httpx.HTTPError) as exc:
        _flash(request, f"ลบไฟล์ไม่สำเร็จ: {exc}", "error")
        return RedirectResponse("/admin/knowledge", status_code=HTTP_303_SEE_OTHER)

    index_result = await run_in_threadpool(pipeline.remove_document, safe)

    if not removed_file and not index_result["ok"]:
        _flash(request, f"ไม่พบเอกสาร “{safe}” ทั้งในคลังและใน index ค่ะ", "error")
    else:
        log.info("ลบเอกสาร %s (โดย %s)", safe, user.username)
        detail = (
            f" และเอาออกจาก index แล้ว ({index_result['removed']} chunk)"
            if index_result["ok"]
            else " (ไม่ได้อยู่ใน index อยู่แล้ว)"
        )
        _flash(request, f"ลบเอกสาร “{safe}” ออกจากคลัง{detail}")

    return RedirectResponse("/admin/knowledge", status_code=HTTP_303_SEE_OTHER)


@app.post("/admin/knowledge/index-remove")
async def knowledge_index_remove(
    request: Request,
    name: str = Form(...),
    user: auth.User = Depends(require_admin),
):
    """เอาเอกสารออกจาก index อย่างเดียว — ใช้กับรายการที่ไม่มีไฟล์ต้นฉบับแล้ว"""
    try:
        safe = docstore.safe_name(name)
    except docstore.DocError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/admin/knowledge", status_code=HTTP_303_SEE_OTHER)

    result = await run_in_threadpool(pipeline.remove_document, safe)
    _flash(request, result["message"], "ok" if result["ok"] else "error")
    if result["ok"]:
        log.info("เอา %s ออกจาก index (โดย %s)", safe, user.username)
    return RedirectResponse("/admin/knowledge", status_code=HTTP_303_SEE_OTHER)


@app.post("/admin/knowledge/reindex")
async def knowledge_reindex(
    request: Request,
    mode: str = Form("merge"),
    user: auth.User = Depends(require_admin),
):
    """สร้าง index จากไฟล์ในคลังปัจจุบัน

    merge   = อัปเดตเฉพาะไฟล์ที่มีอยู่ตอนนี้ ทับของชื่อเดิม เอกสารอื่นคงไว้
    replace = ล้าง index เดิมทิ้งแล้วสร้างใหม่จากไฟล์ในคลังล้วน ๆ
              ใช้ตอนที่ index มีเศษของเอกสารที่ไม่มีไฟล์แล้วปนอยู่
    """
    try:
        result = await run_in_threadpool(
            partial(pipeline.ingest, replace=mode == "replace")
        )
    except Exception as exc:  # noqa: BLE001 — เอกสารเยอะจนเกินเพดานเวลาเป็นเรื่องปกติ
        log.error("สร้าง index จากหน้าคลังความรู้ไม่สำเร็จ: %s", exc)
        _flash(
            request,
            "สร้าง index ไม่สำเร็จค่ะ (อาจเพราะเอกสารเยอะเกินเวลาที่เซิร์ฟเวอร์ให้ต่อคำขอ) "
            "ลองกดใหม่อีกครั้ง หรือรันในเครื่องแทน: "
            "python scripts/pull_docs.py แล้วตามด้วย python scripts/ingest.py --push",
            "error",
        )
        return RedirectResponse("/admin/knowledge", status_code=HTTP_303_SEE_OTHER)

    message = result["message"]
    if result.get("failed"):
        message += " · อ่านไม่ได้: " + ", ".join(f["name"] for f in result["failed"])
    _flash(request, message, "ok" if result["ok"] else "error")
    return RedirectResponse("/admin/knowledge", status_code=HTTP_303_SEE_OTHER)


@app.get("/admin/users")
async def users_page(request: Request, user: auth.User = Depends(require_admin)):
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "title": config.APP_TITLE,
            "user": user,
            "users": auth.list_users(),
            "flash": _take_flash(request),
            "min_password": auth.MIN_PASSWORD,
            # เขียนดิสก์ไม่ได้แต่มี Postgres ก็จัดการบัญชีได้ตามปกติ ไม่ต้องเตือน
            "read_only": config.READ_ONLY_FS and not db.enabled(),
            "users_file_error": auth.users_file_error,
        },
    )


@app.post("/admin/users/create")
async def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    role: str = Form(auth.ROLE_MEMBER),
    user: auth.User = Depends(require_admin),
):
    try:
        created = await run_in_threadpool(
            auth.create_user, username, password, confirm, role
        )
        _flash(request, f"สร้างบัญชี “{created.username}” สิทธิ์"
                        f"{'ผู้ดูแลระบบ' if created.is_admin else 'สมาชิก'}แล้ว")
    except auth.AuthError as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/admin/users", status_code=HTTP_303_SEE_OTHER)


@app.post("/admin/users/password")
async def users_password(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    user: auth.User = Depends(require_admin),
):
    try:
        await run_in_threadpool(auth.set_password, username, password, confirm)
        _flash(request, f"เปลี่ยนรหัสผ่านของ “{username}” แล้ว")
    except auth.AuthError as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/admin/users", status_code=HTTP_303_SEE_OTHER)


@app.post("/admin/users/role")
async def users_role(
    request: Request,
    username: str = Form(...),
    role: str = Form(...),
    user: auth.User = Depends(require_admin),
):
    try:
        await run_in_threadpool(auth.set_role, username, role, actor=user.username)
        _flash(request, f"เปลี่ยนสิทธิ์ “{username}” แล้ว")
    except auth.AuthError as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/admin/users", status_code=HTTP_303_SEE_OTHER)


@app.post("/admin/users/delete")
async def users_delete(
    request: Request,
    username: str = Form(...),
    user: auth.User = Depends(require_admin),
):
    try:
        await run_in_threadpool(auth.delete_user, username, actor=user.username)
        _flash(request, f"ลบบัญชี “{username}” แล้ว")
    except auth.AuthError as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/admin/users", status_code=HTTP_303_SEE_OTHER)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# หน้าเว็บหลัก
# --------------------------------------------------------------------------- #
@app.get("/")
async def index(request: Request):
    """หน้าถาม-ตอบ — เปิดให้ทุกคนที่มีลิงก์ใช้ได้ ไม่ต้องล็อกอิน

    ล็อกอินมีไว้สำหรับผู้ดูแลระบบเพื่อจัดการเอกสารและบัญชีเท่านั้น
    """
    user = current_user(request)

    # ยังไม่มีผู้ดูแลระบบเลย -> พาไปตั้งบัญชีแรกก่อน
    if user is None and not auth.has_any_user():
        return RedirectResponse("/register", status_code=HTTP_303_SEE_OTHER)

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
            # ชื่อไฟล์แสดงเฉพาะแอดมิน (template กรองอีกชั้นด้วย can_manage)
            "sources": store.sources() if (user and user.is_admin) else [],
            "document_count": len(store.sources()),
            "built_at": store.built_at,
            "user": user,
            "can_manage": bool(user and user.is_admin),
            "show_sources": config.SHOW_SOURCES,
        },
    )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/status")
async def status(request: Request):
    """สถานะระบบสำหรับหน้าเว็บ — เปิดให้ทุกคนเพราะหน้าถาม-ตอบไม่ต้องล็อกอิน"""
    user = current_user(request)
    is_admin = bool(user and user.is_admin)
    store = pipeline.get_store()
    sources = store.sources()
    return {
        "user": ({"username": user.username, "role": user.role} if user else None),
        "has_api_key": config.has_api_key(),
        "chat_model": config.GEMINI_CHAT_MODEL,
        "embed_model": config.GEMINI_EMBED_MODEL,
        "embed_backend": store.backend,
        "chunk_count": len(store.chunks),
        # ชื่อไฟล์เป็นชื่อเอกสารภายใน ส่งให้แอดมินเท่านั้น
        # ส่วนจำนวนเอกสารส่งได้ ไม่ได้บอกอะไรที่อ่อนไหว
        "sources": sources if is_admin else [],
        "document_count": len(sources),
        "built_at": store.built_at,
        "top_k": config.TOP_K,
        "can_manage": is_admin,
        # ที่มาของ index มีผลกับ "กด ingest แล้วทำไมไม่เปลี่ยน" — แอดมินต้องเห็น
        "index_source": pipeline.index_source if is_admin else None,
        "user_storage": auth.storage_backend() if is_admin else None,
    }


@app.post("/api/ask")
async def api_ask(request: Request, payload: AskRequest):
    """ถามคำถาม — เปิดให้ทุกคนที่มีลิงก์ ไม่ต้องล็อกอิน

    มีการจำกัดจำนวนคำถามต่อ IP เพราะทุกคำถามใช้โควตา Gemini
    ถ้าไม่จำกัด ลิงก์หลุดครั้งเดียวโควตาหมดได้ในไม่กี่นาที
    """
    user = current_user(request)
    who = user.username if user else f"ไม่ล็อกอิน/{_client_ip(request)}"

    # ผู้ดูแลระบบไม่ถูกจำกัด
    if user is None:
        wait = _rate_limit_check(_client_ip(request))
        if wait:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"ถามถี่เกินไปค่ะ รบกวนรออีกประมาณ {wait // 60 + 1} นาทีแล้วลองใหม่ "
                    "หรือสอบถามเจ้าหน้าที่โดยตรงได้เลยนะคะ"
                ),
            )

    history = [t.model_dump() for t in payload.history]
    # งาน embed/generate เป็น blocking I/O -> โยนเข้า threadpool กัน event loop ค้าง
    result = await run_in_threadpool(
        pipeline.ask, payload.question, history, payload.top_k
    )
    log.info("ถาม (%s): %s", who, payload.question[:80])
    # เก็บคำถามที่ตอบไม่ได้ไว้ดูว่าคลังความรู้ยังขาดเรื่องอะไร (เงียบถ้าไม่มีฐานข้อมูล)
    await run_in_threadpool(
        gaps.record, payload.question, result, user.username if user else None
    )
    return result


@app.post("/api/ingest")
async def api_ingest(replace: bool = False, user: auth.User = Depends(require_admin)):
    # บน serverless ดิสก์เขียนไม่ได้ — ถ้าตั้ง Blob ไว้แล้ว pipeline.ingest() จะดึงเอกสาร
    # จาก Blob มาประมวลผลที่ /tmp แล้ว push index กลับขึ้น Blob ให้เองในคำขอเดียว
    # (กด "สร้าง Index" จากหน้าเว็บได้ตรง ๆ ไม่ต้องเปิดเครื่องรันสคริปต์)
    # ต้องมี Blob ตั้งไว้ก่อนเท่านั้น ไม่งั้นไม่มีที่เก็บทั้งเอกสารต้นฉบับและ index เลย
    if config.READ_ONLY_FS and not objectstore.enabled():
        raise HTTPException(
            status_code=409,
            detail=(
                "เซิร์ฟเวอร์เขียนไฟล์ไม่ได้ และยังไม่มีที่เก็บถาวรค่ะ — "
                "ต้องตั้ง DATABASE_URL หรือ BLOB_READ_WRITE_TOKEN อย่างน้อยหนึ่งอย่างก่อน"
            ),
        )

    try:
        result = await run_in_threadpool(partial(pipeline.ingest, replace=replace))
    except Exception as exc:  # noqa: BLE001 — กันเอกสารเยอะจนเกินเพดานเวลาแล้วเว็บพัง 500 เปล่า ๆ
        log.error("สร้าง index ผ่านเว็บไม่สำเร็จ: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=(
                "สร้าง index ผ่านเว็บไม่สำเร็จค่ะ (อาจเพราะเอกสารเยอะเกินเวลาที่เซิร์ฟเวอร์ให้ต่อคำขอ) "
                "ลองกดใหม่อีกครั้ง หรือถ้าเอกสารเยอะมาก ให้รันในเครื่องแทน: "
                "python scripts/pull_docs.py แล้วตามด้วย python scripts/ingest.py --push"
            ),
        ) from exc

    if not result["ok"]:
        return JSONResponse(result, status_code=422)
    return result


class RemoveRequest(BaseModel):
    source: str = Field(min_length=1, max_length=400)


@app.post("/api/sources/remove")
async def api_remove_source(
    payload: RemoveRequest, user: auth.User = Depends(require_admin)
):
    """เอาเอกสารหนึ่งไฟล์ออกจาก index

    จำเป็นเพราะ ingest เป็นแบบเพิ่มเข้าไปแล้ว การลบไฟล์ออกจากคลังเอกสาร
    จะไม่ทำให้มันหายจาก index เองอีกต่อไป
    """
    if config.READ_ONLY_FS and not objectstore.enabled():
        raise HTTPException(
            status_code=409,
            detail="เซิร์ฟเวอร์เขียนไฟล์ไม่ได้ และยังไม่มีที่เก็บถาวรค่ะ",
        )

    result = await run_in_threadpool(pipeline.remove_document, payload.source)
    if not result["ok"]:
        return JSONResponse(result, status_code=404)
    log.info("ลบเอกสารออกจาก index: %s (โดย %s)", payload.source, user.username)
    return result


@app.post("/api/upload")
async def api_upload(
    files: list[UploadFile] = File(...),
    user: auth.User = Depends(require_admin),
):
    """รับเอกสารเข้าคลัง (ยังไม่สร้าง index)

    docstore เป็นคนตัดสินว่าไฟล์ไปลง data/ ในเครื่อง หรือขึ้น objectstore ใต้ docs/
    หน้าเว็บจึงไม่ต้องรู้ว่ารันอยู่บนอะไร — ดูและแก้ไฟล์ต่อได้ที่ /admin/knowledge
    """
    if not docstore.writable():
        raise HTTPException(
            status_code=409,
            detail=(
                "เซิร์ฟเวอร์เขียนไฟล์ไม่ได้ และยังไม่มีที่เก็บถาวรค่ะ — "
                "ต้องตั้ง DATABASE_URL หรือ BLOB_READ_WRITE_TOKEN อย่างน้อยหนึ่งอย่างก่อน"
            ),
        )

    saved: list[str] = []
    rejected: list[dict] = []

    for upload in files:
        raw_name = (upload.filename or "").strip()
        if not raw_name:
            continue
        try:
            name = docstore.safe_name(raw_name)
            docstore.check_supported(name)
            data = await run_in_threadpool(upload.file.read)
            await run_in_threadpool(
                docstore.save, name, data,
                upload.content_type or "application/octet-stream",
            )
            saved.append(name)
        except (docstore.DocError, OSError, httpx.HTTPError) as exc:
            rejected.append({"name": raw_name, "reason": str(exc)})
        finally:
            await upload.close()

    if not saved and rejected:
        raise HTTPException(status_code=422, detail={"saved": saved, "rejected": rejected})

    next_step = "กด “สร้าง Index” เพื่อให้ระบบเรียนรู้"
    return {
        "ok": True,
        "saved": saved,
        "rejected": rejected,
        "staged_to_blob": docstore.use_remote(),
        "message": f"อัปโหลด {len(saved)} ไฟล์แล้ว — {next_step}",
    }


@app.get("/api/gaps")
async def api_gaps(limit: int = 50, user: auth.User = Depends(require_admin)):
    """คำถามที่ระบบตอบไม่ได้ — ใช้ตัดสินใจว่าควรเพิ่มเอกสารเรื่องอะไรต่อ"""
    if not db.enabled():
        raise HTTPException(
            status_code=409,
            detail="ยังไม่ได้ตั้ง DATABASE_URL จึงยังไม่ได้เก็บคำถามที่ตอบไม่ได้",
        )
    return {"gaps": await run_in_threadpool(gaps.top, max(1, min(limit, 200)))}


@app.get("/api/users")
async def api_users(user: auth.User = Depends(require_admin)):
    """ดูรายชื่อสมาชิก — เฉพาะ admin"""
    return {"users": auth.list_users()}


@app.get("/healthz")
async def healthz():
    """ตรวจสุขภาพระบบ — บอกว่าตั้งค่าอะไรครบ/ขาด โดยไม่เผยค่าความลับ

    เปิดดูได้โดยไม่ต้องล็อกอิน เพราะต้องใช้ไล่ปัญหาตอน deploy
    คืนเฉพาะ true/false และตัวเลข ไม่มีคีย์ ไม่มีชื่อคน
    """
    store = pipeline.get_store()
    problems: list[str] = []

    # อ่านค่าครั้งเดียวแล้วใช้ทั้ง problems และ checks
    # ถ้าเรียกซ้ำสองรอบ อาจได้คนละคำตอบเมื่อ cache อุ่นขึ้นระหว่างนั้น
    # แล้วรายงานขัดกันเอง (checks บอกพร้อม แต่ problems บอกยังไม่ได้ตั้ง)
    has_key = config.has_api_key()
    has_secret = auth.has_session_secret()

    if not has_key:
        problems.append("ยังไม่ได้ตั้ง GEMINI_API_KEY -> ตอบคำถามไม่ได้")
    if not has_secret:
        problems.append(
            "ยังไม่ได้ตั้ง SESSION_SECRET -> ล็อกอินไม่สำเร็จ (คุกกี้ใช้ต่อไม่ได้)"
            if config.READ_ONLY_FS
            else "ยังไม่ได้ตั้ง SESSION_SECRET -> ผู้ใช้จะหลุดออกจากระบบเมื่อรีสตาร์ท"
        )
    if not store.chunks:
        problems.append("ไม่มี index -> ไม่มีความรู้ให้ตอบ")

    # เช็คไฟล์ผู้ใช้เสียหายก่อน เพราะอาการจะเหมือน "ไม่มีใครสมัคร" ซึ่งชี้ผิดทาง
    user_count = auth.user_count()
    if auth.users_file_error:
        problems.append(f"⚠️ {auth.users_file_error} -> ทุกคนล็อกอินไม่ได้")
    elif not auth.has_any_user():
        problems.append(
            "ไม่มีบัญชีผู้ใช้ และเซิร์ฟเวอร์เขียนไฟล์ไม่ได้ -> สมัครสมาชิกไม่ได้"
            if config.READ_ONLY_FS and not db.enabled()
            else "ไม่มีบัญชีผู้ใช้ -> คนแรกที่สมัครจะได้สิทธิ์ admin"
        )
    if config.READ_ONLY_FS and not config.SESSION_HTTPS_ONLY:
        problems.append("แนะนำให้ตั้ง SESSION_HTTPS_ONLY=1 เมื่อรันบน https")

    # เขียนดิสก์ไม่ได้ + ไม่มีที่เก็บถาวร = ข้อมูลหายทุกครั้งที่ instance ตื่นใหม่
    if config.READ_ONLY_FS and not db.enabled():
        problems.append(
            "ยังไม่ได้ตั้ง DATABASE_URL บนเซิร์ฟเวอร์ที่เขียนไฟล์ไม่ได้ -> สมัคร/แก้บัญชีไม่ได้"
        )
    if config.READ_ONLY_FS and not objectstore.enabled():
        problems.append(
            "ไม่มีที่เก็บถาวร (DATABASE_URL หรือ BLOB_READ_WRITE_TOKEN) "
            "-> อัปเดตความรู้ได้เฉพาะตอน deploy ใหม่เท่านั้น"
        )

    return {
        "status": "ok" if not problems else "misconfigured",
        "checks": {
            "gemini_api_key": has_key,
            "session_secret": has_secret,
            "session_https_only": config.SESSION_HTTPS_ONLY,
            "read_only_filesystem": config.READ_ONLY_FS,
            "index_chunks": len(store.chunks),
            "index_documents": len(store.sources()),
            "index_source": pipeline.index_source,
            "embed_backend": store.backend,
            "user_storage": auth.storage_backend(),
            "object_storage": objectstore.backend(),
            "user_accounts": user_count,
            "users_file_ok": not auth.users_file_error,
            "chat_model": config.GEMINI_CHAT_MODEL,
        },
        "problems": problems,
    }
