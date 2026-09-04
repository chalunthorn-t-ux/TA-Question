"""ที่เก็บไฟล์ก้อนใหญ่ (index + เอกสารต้นฉบับ) — Vercel Blob หรือ Postgres

มีสองหลังบ้านเพราะการตั้ง BLOB_READ_WRITE_TOKEN บน Vercel มีขั้นตอนเยอะและพลาดง่าย
ส่วน DATABASE_URL มักถูกตั้งให้อัตโนมัติตอนผูก Postgres อยู่แล้ว
ถ้ามี Postgres อย่างเดียวก็ควรใช้งานได้ ไม่ควรติดตายเพราะยังไม่ได้ตั้ง Blob

    มี BLOB_READ_WRITE_TOKEN  -> ใช้ Vercel Blob (เหมาะกับไฟล์เยอะ/ใหญ่)
    มีแต่ DATABASE_URL        -> เก็บเป็น bytea ใน Postgres
    ไม่มีทั้งคู่              -> ปิดการใช้งาน (รันในเครื่องก็ใช้ดิสก์ตามปกติ)

index ทั้งก้อนราว 60 KB และเอกสารหลัก MB ซึ่ง Neon free tier (0.5 GB) รับไหวสบาย
"""

from __future__ import annotations

import logging

from . import blobstore, db

log = logging.getLogger(__name__)


def backend() -> str:
    if blobstore.enabled():
        return "blob"
    if db.enabled():
        return "postgres"
    return "none"


def enabled() -> bool:
    return backend() != "none"


# --------------------------------------------------------------------------- #
# หลังบ้าน: Postgres (ตาราง app_files)
# --------------------------------------------------------------------------- #
def _pg_put(key: str, data: bytes) -> str:
    db.ensure_schema()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO app_files (key, data, size, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (key) DO UPDATE SET
                data = EXCLUDED.data, size = EXCLUDED.size, updated_at = now()
            """,
            (key, data, len(data)),
        )
    return f"postgres://app_files/{key}"


def _pg_get(key: str) -> bytes | None:
    if not db.ensure_schema():
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT data FROM app_files WHERE key = %s", (key,)).fetchone()
    return bytes(row[0]) if row else None


def _pg_list(subprefix: str) -> list[dict]:
    if not db.ensure_schema():
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT key, size, updated_at FROM app_files WHERE key LIKE %s ORDER BY key",
            (f"{subprefix}%",),
        ).fetchall()
    return [
        {
            "key": r[0],
            "pathname": r[0],
            "size": r[1],
            "modified": r[2].isoformat(timespec="seconds") if r[2] else "",
            "url": "",
        }
        for r in rows
    ]


def _pg_delete(key: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM app_files WHERE key = %s", (key,))


# --------------------------------------------------------------------------- #
# API ที่ส่วนอื่นเรียกใช้ — หน้าตาเหมือน blobstore เดิม
# --------------------------------------------------------------------------- #
def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    if blobstore.enabled():
        return blobstore.put(key, data, content_type)
    if db.enabled():
        return _pg_put(key, data)
    raise RuntimeError(
        "ยังไม่มีที่เก็บไฟล์ — ต้องตั้ง DATABASE_URL หรือ BLOB_READ_WRITE_TOKEN อย่างน้อยหนึ่งอย่าง"
    )


def get(key: str, *, fresh: bool = False) -> bytes | None:
    if blobstore.enabled():
        return blobstore.get(key, fresh=fresh)
    if db.enabled():
        # Postgres อ่านของล่าสุดเสมอ ไม่มี CDN มาคั่น จึงไม่ต้องสน fresh
        return _pg_get(key)
    return None


def list_keys(subprefix: str = "") -> list[dict]:
    if blobstore.enabled():
        return blobstore.list_keys(subprefix)
    if db.enabled():
        return _pg_list(subprefix)
    return []


def delete(key: str) -> None:
    if blobstore.enabled():
        blobstore.delete(key)
    elif db.enabled():
        _pg_delete(key)
