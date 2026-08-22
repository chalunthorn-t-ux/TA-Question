"""ค่าคอนฟิกกลางของระบบ อ่านจาก .env ทั้งหมด"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


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

for _d in (DATA_DIR, INDEX_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---- Gemini ----
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-3.6-flash").strip()
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001").strip()
GEMINI_EMBED_DIM = _int("GEMINI_EMBED_DIM", 768)
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# ---- RAG ----
CHUNK_SIZE = _int("CHUNK_SIZE", 900)
CHUNK_OVERLAP = _int("CHUNK_OVERLAP", 150)
TOP_K = _int("TOP_K", 5)
MIN_SCORE = _float("MIN_SCORE", 0.15)
HYBRID_ALPHA = _float("HYBRID_ALPHA", 0.7)

# ---- UI ----
APP_TITLE = os.getenv("APP_TITLE", "TA Assistant")
APP_SUBTITLE = os.getenv("APP_SUBTITLE", "ผู้ช่วยตอบคำถามสำหรับผู้ช่วยสอนใหม่")

# ไฟล์ที่รองรับ
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".md"}


def has_api_key() -> bool:
    return bool(GEMINI_API_KEY)
