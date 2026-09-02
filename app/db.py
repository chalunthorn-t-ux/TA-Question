"""การเชื่อมต่อ PostgreSQL (Neon) — ใช้เก็บบัญชีผู้ใช้แทนไฟล์ JSON

บน Vercel ดิสก์เขียนไม่ได้ ไฟล์ storage/users.json จึงใช้ไม่ได้จริง
ตั้ง DATABASE_URL เมื่อไหร่ ระบบจะสลับมาใช้ Postgres ให้อัตโนมัติ
ไม่ตั้งก็ยังทำงานแบบไฟล์ JSON เหมือนเดิม (สะดวกตอนพัฒนาในเครื่อง)

⚠️ บน serverless ต้องใช้ connection string แบบ pooled (ของ Neon จะมี "-pooler")
   เพราะทุก request เปิด connection ใหม่ ถ้าต่อตรงจะชน max_connections เร็วมาก
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager

log = logging.getLogger(__name__)

# Vercel/Neon integration ฉีดตัวแปรมาหลายชื่อ รับไว้ทุกแบบเพื่อไม่ต้องมานั่งตั้งซ้ำ
_URL_KEYS = ("DATABASE_URL", "POSTGRES_URL", "NEON_DATABASE_URL")

_CONNECT_TIMEOUT = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   text PRIMARY KEY,
    password   text NOT NULL,
    role       text NOT NULL DEFAULT 'member',
    created_at timestamptz NOT NULL DEFAULT now(),
    last_login timestamptz
);

CREATE TABLE IF NOT EXISTS unanswered (
    id        bigserial PRIMARY KEY,
    asked_at  timestamptz NOT NULL DEFAULT now(),
    username  text,
    question  text NOT NULL,
    status    text NOT NULL,
    top_score real
);

CREATE INDEX IF NOT EXISTS unanswered_asked_at_idx ON unanswered (asked_at DESC);

CREATE TABLE IF NOT EXISTS app_settings (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_files (
    key        text PRIMARY KEY,
    data       bytea NOT NULL,
    size       bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""


def database_url() -> str:
    for key in _URL_KEYS:
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def enabled() -> bool:
    return bool(database_url())


@contextmanager
def connect():
    """เปิด connection หนึ่งครั้งต่อการใช้งาน (autocommit)

    ไม่ทำ connection pool เองเพราะ serverless instance ตายเมื่อไหร่ก็ได้
    ให้ pooler ฝั่ง Neon จัดการแทน
    """
    import psycopg      # import ในฟังก์ชัน เพื่อให้แอปยังขึ้นได้ถ้ายังไม่ได้ลง psycopg

    conn = psycopg.connect(
        database_url(),
        connect_timeout=_CONNECT_TIMEOUT,
        autocommit=True,
    )
    try:
        yield conn
    finally:
        conn.close()


def init_schema() -> None:
    """สร้างตารางถ้ายังไม่มี — เรียกซ้ำได้ ไม่พัง"""
    with connect() as conn:
        conn.execute(_SCHEMA)
    log.info("เตรียมตารางในฐานข้อมูลเรียบร้อย")


# สร้างตารางครั้งเดียวต่อ process — ต้องเป็นแบบ lazy เพราะโค้ดบางส่วน
# (เช่นคีย์เซ็นคุกกี้) ถูกเรียกตั้งแต่ตอน import ก่อน lifespan จะได้ทำงาน
_schema_ready = False


def ensure_schema() -> bool:
    """เตรียมตารางถ้ายังไม่ได้ทำในรอบนี้ คืน False ถ้าทำไม่สำเร็จ"""
    global _schema_ready

    if _schema_ready:
        return True
    if not enabled():
        return False

    try:
        init_schema()
    except Exception as exc:      # noqa: BLE001 — ต่อฐานข้อมูลไม่ได้ ไม่ควรทำให้แอปล้ม
        log.warning("เตรียมตารางไม่สำเร็จ: %s", exc)
        return False

    _schema_ready = True
    return True


def ping() -> str:
    """คืนข้อความ error ถ้าต่อไม่ได้ คืนค่าว่างถ้าต่อได้"""
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
        return ""
    except Exception as exc:      # noqa: BLE001 — อยากได้ข้อความ ไม่ว่าพังด้วยสาเหตุใด
        return f"ต่อฐานข้อมูลไม่ได้: {exc}"
