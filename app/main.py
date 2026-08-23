"""FastAPI application — เสิร์ฟหน้าเว็บ + API สำหรับถาม-ตอบและจัดการ index

ทุกหน้าและทุก API ต้องเข้าสู่ระบบก่อน ยกเว้นหน้า login/register และ /healthz
งานที่แก้ข้อมูล (อัปโหลดเอกสาร สร้าง index) จำกัดเฉพาะ admin
"""

from __future__ import annotations

import logging
import shutil
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


def _auth_page(request: Request, mode: str, error: str = "", username: str = ""):
    """หน้า login/register — mode คือ 'login' หรือ 'register'"""
    first_user = not auth.has_any_user()
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "title": config.APP_TITLE,
            "subtitle": config.APP_SUBTITLE,
            "mode": mode,
            "error": error,
            "username": username,
            # ยังไม่มีใครในระบบ -> ชวนให้สมัครเป็น admin
            "first_user": first_user,
            "min_password": auth.MIN_PASSWORD,
        },
        status_code=200 if not error else 400,
    )


# --------------------------------------------------------------------------- #
# หน้า login / register
# --------------------------------------------------------------------------- #
@app.get("/login")
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    if not auth.has_any_user():
        return RedirectResponse("/register", status_code=HTTP_303_SEE_OTHER)
    return _auth_page(request, "login")


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
    if current_user(request):
        return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)
    return _auth_page(request, "register")


@app.post("/register")
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
):
    try:
        user = await run_in_threadpool(auth.register, username, password, confirm)
    except auth.AuthError as exc:
        return _auth_page(request, "register", str(exc), username)

    request.session.clear()
    request.session["user"] = {"username": user.username, "role": user.role}
    return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# หน้าเว็บหลัก
# --------------------------------------------------------------------------- #
@app.get("/")
async def index(request: Request):
    user = current_user(request)
    if user is None:
        target = "/register" if not auth.has_any_user() else "/login"
        return RedirectResponse(target, status_code=HTTP_303_SEE_OTHER)

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
            "user": user,
            "can_manage": user.is_admin,
        },
    )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/status")
async def status(user: auth.User = Depends(require_user)):
    store = pipeline.get_store()
    return {
        "user": {"username": user.username, "role": user.role},
        "has_api_key": config.has_api_key(),
        "chat_model": config.GEMINI_CHAT_MODEL,
        "embed_model": config.GEMINI_EMBED_MODEL,
        "embed_backend": store.backend,
        "chunk_count": len(store.chunks),
        "sources": store.sources(),
        "built_at": store.built_at,
        "top_k": config.TOP_K,
        "can_manage": user.is_admin,
    }


@app.post("/api/ask")
async def api_ask(payload: AskRequest, user: auth.User = Depends(require_user)):
    history = [t.model_dump() for t in payload.history]
    # งาน embed/generate เป็น blocking I/O -> โยนเข้า threadpool กัน event loop ค้าง
    result = await run_in_threadpool(
        pipeline.ask, payload.question, history, payload.top_k
    )
    log.info("ถาม (%s): %s", user.username, payload.question[:80])
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
    return {"status": "ok"}
