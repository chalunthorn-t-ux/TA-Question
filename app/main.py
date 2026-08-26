"""FastAPI application — เสิร์ฟหน้าเว็บ + API สำหรับถาม-ตอบและจัดการ index

ทุกหน้าและทุก API ต้องเข้าสู่ระบบก่อน ยกเว้นหน้า login/register และ /healthz
งานที่แก้ข้อมูล (อัปโหลดเอกสาร สร้าง index) จำกัดเฉพาะ admin
"""

from __future__ import annotations

import logging
import shutil
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from . import auth, config
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
            "read_only": config.READ_ONLY_FS,
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
                    f"ถามถี่เกินไปครับ รบกวนรออีกประมาณ {wait // 60 + 1} นาทีแล้วลองใหม่ "
                    "หรือสอบถามเจ้าหน้าที่โดยตรงได้เลยนะครับ"
                ),
            )

    history = [t.model_dump() for t in payload.history]
    # งาน embed/generate เป็น blocking I/O -> โยนเข้า threadpool กัน event loop ค้าง
    result = await run_in_threadpool(
        pipeline.ask, payload.question, history, payload.top_k
    )
    log.info("ถาม (%s): %s", who, payload.question[:80])
    return result


@app.post("/api/ingest")
async def api_ingest(user: auth.User = Depends(require_admin)):
    result = await run_in_threadpool(pipeline.ingest)
    if not result["ok"]:
        return JSONResponse(result, status_code=422)
    return result


@app.post("/api/upload")
async def api_upload(
    files: list[UploadFile] = File(...),
    user: auth.User = Depends(require_admin),
):
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

    if not config.has_api_key():
        problems.append("ยังไม่ได้ตั้ง GEMINI_API_KEY -> ตอบคำถามไม่ได้")
    if not auth.has_session_secret():
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
            if config.READ_ONLY_FS
            else "ไม่มีบัญชีผู้ใช้ -> คนแรกที่สมัครจะได้สิทธิ์ admin"
        )
    if config.READ_ONLY_FS and not config.SESSION_HTTPS_ONLY:
        problems.append("แนะนำให้ตั้ง SESSION_HTTPS_ONLY=1 เมื่อรันบน https")

    return {
        "status": "ok" if not problems else "misconfigured",
        "checks": {
            "gemini_api_key": config.has_api_key(),
            "session_secret": auth.has_session_secret(),
            "session_https_only": config.SESSION_HTTPS_ONLY,
            "read_only_filesystem": config.READ_ONLY_FS,
            "index_chunks": len(store.chunks),
            "index_documents": len(store.sources()),
            "embed_backend": store.backend,
            "user_accounts": user_count,
            "users_file_ok": not auth.users_file_error,
            "chat_model": config.GEMINI_CHAT_MODEL,
        },
        "problems": problems,
    }
