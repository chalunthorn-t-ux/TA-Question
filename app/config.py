"""ค่าคอนฟิกกลางของระบบ อ่านจาก .env ทั้งหมด"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _str(key: str, default: str) -> str:
    """อ่าน env var แบบถือว่า "ตั้งเป็นค่าว่าง" = ยังไม่ได้ตั้ง

    บนบริการอย่าง Vercel เผลอสร้างตัวแปรทิ้งไว้โดยไม่ใส่ค่าได้ง่าย
    ถ้าไม่ถอยไปใช้ค่าเริ่มต้น จะกลายเป็นเรียกโมเดลชื่อว่างเปล่าแล้วพังแบบงง ๆ
    """
    return (os.getenv(key) or "").strip() or default


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except ValueError:
        return default


# ---- โฟลเดอร์ ----
DATA_DIR = BASE_DIR / "data"          # วางเอกสารต้นฉบับที่นี่
INDEX_DIR = BASE_DIR / "storage"      # เก็บ index ที่สร้างแล้ว
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"

# สร้างโฟลเดอร์ให้ถ้ายังไม่มี — แต่บน serverless (Vercel) ดิสก์อ่านได้เท่านั้น
# ต้องไม่ให้ล้มตอน import ไม่งั้นแอปไม่ขึ้นเลย
READ_ONLY_FS = False
for _d in (DATA_DIR, INDEX_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        READ_ONLY_FS = True

# ---- Gemini ----
# ค่าที่ตั้งไว้ตอน import — ใช้ผ่าน gemini_api_key() เสมอ เพราะมีทางเลือกจากฐานข้อมูลด้วย
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_CHAT_MODEL = _str("GEMINI_CHAT_MODEL", "gemini-3.6-flash")
GEMINI_EMBED_MODEL = _str("GEMINI_EMBED_MODEL", "gemini-embedding-001")
GEMINI_EMBED_DIM = _int("GEMINI_EMBED_DIM", 768)
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# โมเดล gemini-3.x "คิด" ก่อนตอบ ซึ่งกินเวลาส่วนใหญ่ (วัดได้ ~1,300 tokens = ~11 วินาที)
# จำกัดไว้ให้เร็วขึ้นโดยคำตอบยังครบ ตั้ง -1 เพื่อไม่ส่งค่านี้ (ปล่อยให้โมเดลตัดสินใจเอง)
GEMINI_THINKING_BUDGET = _int("GEMINI_THINKING_BUDGET", 512)

# ---- งบเวลาต่อคำถาม ----
# serverless มีเพดานเวลา ถ้าเลยแล้วจะถูกตัดทิ้งกลางทาง ผู้ใช้ไม่ได้อะไรเลย (504)
# ตั้งงบให้ต่ำกว่าเพดานจริง เพื่อให้ระบบยอมแพ้ทันแล้วคืนข้อความจากเอกสารให้อ่านแทน
# ต้องน้อยกว่า maxDuration ใน vercel.json
REQUEST_BUDGET_SECONDS = _int("REQUEST_BUDGET_SECONDS", 45)

# ---- จำกัดจำนวนคำถามของผู้ใช้ที่ไม่ได้ล็อกอิน ----
# หน้าถาม-ตอบเปิดให้ทุกคนที่มีลิงก์ ทุกคำถามใช้โควตา Gemini ของเจ้าของระบบ
# ตั้ง 0 เพื่อปิดการจำกัด (ไม่แนะนำถ้าลิงก์กระจายออกไปแล้ว)
ASK_RATE_LIMIT = _int("ASK_RATE_LIMIT", 30)          # จำนวนคำถามต่อ IP
ASK_RATE_WINDOW = _int("ASK_RATE_WINDOW", 3600)      # ต่อช่วงเวลา (วินาที)

# ---- ลบชื่อบุคคลก่อนเข้า index ----
# เอกสารมีชื่อผู้เข้าอบรมจริงพร้อมผลประเมิน ซึ่งไม่ควรอยู่ในระบบที่เปิดสาธารณะ
REDACT_NAMES = _str("REDACT_NAMES", "1") not in ("0", "false", "no")
# ชื่อเจ้าหน้าที่ที่ต้องคงไว้ (คั่นด้วยจุลภาค) ไม่งั้นระบบจะตอบไม่ได้ว่าติดต่อใคร
REDACT_KEEP_NAMES = _str("REDACT_KEEP_NAMES", "ทีน")

# ---- RAG ----
CHUNK_SIZE = _int("CHUNK_SIZE", 900)
CHUNK_OVERLAP = _int("CHUNK_OVERLAP", 150)
TOP_K = _int("TOP_K", 5)
MIN_SCORE = _float("MIN_SCORE", 0.15)
HYBRID_ALPHA = _float("HYBRID_ALPHA", 0.7)

# ---- Session / ความปลอดภัย ----
# อายุคุกกี้ล็อกอิน (วินาที) ค่าเริ่มต้น 7 วัน
SESSION_MAX_AGE = _int("SESSION_MAX_AGE", 7 * 24 * 3600)
# ไม่ให้คุกกี้ล็อกอินส่งผ่าน http
# ไม่ได้ตั้งไว้ -> เดาจากสภาพแวดล้อม: เขียนดิสก์ไม่ได้ = serverless = อยู่บน https อยู่แล้ว
# (ตั้งเป็น 0 บังคับปิดได้ ถ้ารัน serverless หลัง proxy ที่เป็น http จริง ๆ)
_https_only_raw = os.getenv("SESSION_HTTPS_ONLY", "").strip().lower()
if _https_only_raw:
    SESSION_HTTPS_ONLY = _https_only_raw in ("1", "true", "yes")
else:
    SESSION_HTTPS_ONLY = READ_ONLY_FS

# หน้า /docs /redoc เปิดดูได้โดยไม่ต้องล็อกอิน จึงปิดไว้เป็นค่าเริ่มต้น
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "").strip() in ("1", "true", "yes")

# ---- UI ----
# แสดงการ์ดแหล่งอ้างอิงพร้อมข้อความจากเอกสารใต้คำตอบหรือไม่
# ปิดไว้เป็นค่าเริ่มต้น: หน้าเว็บเปิดให้คนทั่วไป ไม่ควรโชว์เนื้อหาดิบจากเอกสารภายใน
# เปิดด้วย SHOW_SOURCES=1 เวลาต้องการตรวจว่าระบบดึงข้อมูลจากที่ไหน
SHOW_SOURCES = _str("SHOW_SOURCES", "0") in ("1", "true", "yes")

APP_TITLE = _str("APP_TITLE", "TA Assistant")
APP_SUBTITLE = _str("APP_SUBTITLE", "ผู้ช่วยตอบคำถามสำหรับ TA (Training Assistant) ใหม่")

# ไฟล์ที่รองรับ
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".md"}


def gemini_api_key() -> str:
    """คีย์ Gemini — env ชนะเสมอ ถ้าไม่ได้ตั้งค่อยดูค่าที่บันทึกไว้ในฐานข้อมูล

    มีทางที่สองเพราะการตั้ง env บน Vercel ต้อง redeploy และแก้ทีหลังไม่สะดวก
    ส่วนค่าในฐานข้อมูลตั้งได้จากหน้า /admin/settings แล้วมีผลทันที
    """
    if GEMINI_API_KEY:
        return GEMINI_API_KEY

    # import ที่นี่เพื่อไม่ให้ config ต้องพึ่งฐานข้อมูลตอน import
    from . import settings_repo

    return settings_repo.get(settings_repo.KEY_GEMINI_API_KEY)


def has_api_key() -> bool:
    return bool(gemini_api_key())
