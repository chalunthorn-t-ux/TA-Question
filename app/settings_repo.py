"""ค่าตั้งระบบที่เก็บในฐานข้อมูล — ทางเลือกแทน environment variable

ตั้ง env บนผู้ให้บริการอย่าง Vercel มีข้อจำกัดที่เจ็บจริง:
ค่าที่ทำเป็น Sensitive แล้วอ่านกลับไม่ได้อีกเลย ทำให้เอามาใช้ในเครื่องไม่ได้
และทุกครั้งที่แก้ต้อง redeploy ใหม่

ค่าที่เก็บที่นี่แก้ได้จากหน้าเว็บ มีผลทันที ไม่ต้อง deploy
env ยังชนะเสมอถ้าตั้งไว้ — ที่นี่เป็นตัวสำรองเมื่อ env ว่าง
"""

from __future__ import annotations

import logging
import time

from . import db

log = logging.getLogger(__name__)

# คีย์ที่รองรับ — จำกัดไว้ไม่ให้กลายเป็นถังขยะ
KEY_SESSION_SECRET = "session_secret"
KEY_GEMINI_API_KEY = "gemini_api_key"

_ALLOWED = {KEY_SESSION_SECRET, KEY_GEMINI_API_KEY}

# อ่านบ่อยมาก (ทุกคำถามเรียก has_api_key) จึงจำไว้สั้น ๆ
# ไม่งั้นกลายเป็นยิง query เพิ่มทุก request
_CACHE_TTL = 30.0
_cache: dict[str, str] = {}
_cache_at: float = 0.0


def available() -> bool:
    return db.enabled()


def _refresh() -> None:
    global _cache, _cache_at

    if not db.ensure_schema():
        _cache = {}
        _cache_at = time.monotonic()
        return

    try:
        with db.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        _cache = {r[0]: r[1] for r in rows}
    except Exception as exc:      # noqa: BLE001 — อ่านค่าตั้งไม่ได้ ไม่ควรทำให้แอปล้ม
        log.warning("อ่านค่าตั้งจากฐานข้อมูลไม่ได้: %s", exc)
        _cache = {}
    _cache_at = time.monotonic()


def get(key: str) -> str:
    if not db.enabled():
        return ""
    if time.monotonic() - _cache_at > _CACHE_TTL:
        _refresh()
    return _cache.get(key, "")


def set(key: str, value: str) -> None:
    global _cache_at

    if key not in _ALLOWED:
        raise ValueError(f"ไม่รู้จักค่าตั้งชื่อ '{key}'")
    if not db.enabled():
        raise RuntimeError("ยังไม่ได้ตั้ง DATABASE_URL จึงบันทึกค่าตั้งไม่ได้")
    if not db.ensure_schema():
        raise RuntimeError("เตรียมตารางในฐานข้อมูลไม่สำเร็จ จึงบันทึกค่าตั้งไม่ได้")

    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, value),
        )
    _cache_at = 0.0      # บังคับให้อ่านใหม่รอบหน้า


def get_or_create(key: str, factory) -> str:
    """อ่านค่า ถ้ายังไม่มีให้สร้างแล้วเก็บไว้ ใช้กับคีย์ที่ต้องคงที่ตลอดไป"""
    existing = get(key)
    if existing:
        return existing

    value = factory()
    try:
        set(key, value)
    except Exception as exc:      # noqa: BLE001
        log.warning("บันทึกค่าตั้ง %s ไม่ได้: %s", key, exc)
        return value

    log.info("สร้างค่าตั้ง %s ใหม่และเก็บลงฐานข้อมูลแล้ว", key)
    return value


def status() -> dict:
    """คีย์ไหนถูกตั้งไว้บ้าง และมาจากไหน — ไม่คืนค่าจริงออกไป"""
    return {key: bool(get(key)) for key in sorted(_ALLOWED)}
