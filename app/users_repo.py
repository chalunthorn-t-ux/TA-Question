"""ชั้นเก็บบัญชีผู้ใช้ — สลับระหว่างไฟล์ JSON (ในเครื่อง) กับ Postgres (production)

แยกออกมาจาก auth.py เพื่อให้ตรรกะเรื่องรหัสผ่าน/สิทธิ์ไม่ต้องรู้ว่าข้อมูลอยู่ที่ไหน
รูป record เหมือนกันทั้งสองหลังบ้าน:

    {"password": "pbkdf2_sha256$...", "role": "admin", "created_at": "...", "last_login": "..."}

เลือกหลังบ้านอัตโนมัติจากการมี DATABASE_URL — ไม่มีตัวเลือกให้ตั้งผิด
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from . import config, db

log = logging.getLogger(__name__)

_USERS_FILE = "users.json"

# ข้อผิดพลาดล่าสุดของชั้นเก็บข้อมูล — แสดงใน /healthz และหน้า /admin/users
# ถ้าปล่อยให้คืนค่าว่างเงียบ ๆ อาการจะเหมือน "ยังไม่มีใครสมัคร" ซึ่งหลอกให้ไล่ผิดทาง
_last_error: str = ""


class RepoError(Exception):
    """เขียนข้อมูลไม่สำเร็จ — ข้อความในนี้แสดงให้ผู้ใช้เห็นได้"""


def backend() -> str:
    return "postgres" if db.enabled() else "json"


def last_error() -> str:
    return _last_error


# --------------------------------------------------------------------------- #
# หลังบ้าน: ไฟล์ JSON
# --------------------------------------------------------------------------- #
def _json_path():
    return config.INDEX_DIR / _USERS_FILE


def _json_load_all() -> dict[str, dict]:
    global _last_error

    path = _json_path()
    if not path.exists():
        _last_error = ""
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _last_error = (
            f"users.json ไม่ใช่ JSON ที่ถูกต้อง "
            f"(บรรทัด {exc.lineno} คอลัมน์ {exc.colno}): {exc.msg}"
        )
        log.critical(
            "ไฟล์ผู้ใช้เสียหาย -> ทุกคนล็อกอินไม่ได้! %s "
            "มักเกิดจากการแก้ไฟล์ด้วยมือแล้ววงเล็บไม่ครบ "
            "ใช้ scripts/users.py จัดการบัญชีแทนการแก้ไฟล์เอง",
            _last_error,
        )
        return {}
    except OSError as exc:
        _last_error = f"อ่านไฟล์ users.json ไม่ได้: {exc}"
        log.critical("อ่านไฟล์ผู้ใช้ไม่ได้ -> ทุกคนล็อกอินไม่ได้! %s", exc)
        return {}

    if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
        _last_error = "users.json มีโครงสร้างไม่ถูกต้อง (ต้องมีคีย์ 'users' เป็นอ็อบเจกต์)"
        log.critical("โครงสร้างไฟล์ผู้ใช้ผิด -> ทุกคนล็อกอินไม่ได้! %s", _last_error)
        return {}

    _last_error = ""
    return data["users"]


def _json_save_all(users: dict[str, dict]) -> None:
    path = _json_path()
    payload = {"version": 1, "users": users}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # เขียนไฟล์ชั่วคราวแล้วเปลี่ยนชื่อ กันไฟล์เสียหายถ้าดับกลางทาง
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise RepoError(
            "ระบบนี้ยังบันทึกบัญชีไม่ได้ (เซิร์ฟเวอร์เขียนไฟล์ไม่ได้ และยังไม่ได้ตั้ง DATABASE_URL) "
            "รบกวนติดต่อผู้ดูแลระบบให้ตั้งค่าฐานข้อมูลก่อนนะคะ"
        ) from exc


# --------------------------------------------------------------------------- #
# หลังบ้าน: Postgres
# --------------------------------------------------------------------------- #
_COLUMNS = "username, password, role, created_at, last_login"


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _row_to_record(row) -> dict:
    return {
        "password": row[1],
        "role": row[2],
        "created_at": _as_text(row[3]),
        "last_login": _as_text(row[4]),
    }


def _pg_fail(exc: Exception, action: str) -> RepoError:
    global _last_error
    _last_error = f"ฐานข้อมูลมีปัญหา: {exc}"
    log.critical("%s ไม่สำเร็จ -> %s", action, exc)
    return RepoError(
        "ระบบติดต่อฐานข้อมูลไม่ได้ในตอนนี้ รบกวนลองใหม่อีกครั้ง "
        "หรือแจ้งผู้ดูแลระบบให้ตรวจค่า DATABASE_URL"
    )


# --------------------------------------------------------------------------- #
# API ที่ auth.py เรียกใช้
# --------------------------------------------------------------------------- #
def load_all() -> dict[str, dict]:
    """คืนบัญชีทั้งหมด — ใช้ตอนแสดงรายชื่อ ไม่ใช่ทางเดินของ read-modify-write"""
    global _last_error

    if not db.enabled():
        return _json_load_all()

    try:
        with db.connect() as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM users ORDER BY username").fetchall()
    except Exception as exc:      # noqa: BLE001
        _last_error = f"อ่านบัญชีจากฐานข้อมูลไม่ได้: {exc}"
        log.critical("อ่านตาราง users ไม่ได้ -> ทุกคนล็อกอินไม่ได้! %s", exc)
        return {}

    _last_error = ""
    return {row[0]: _row_to_record(row) for row in rows}


def get(username: str) -> dict | None:
    global _last_error

    if not db.enabled():
        return _json_load_all().get(username)

    try:
        with db.connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM users WHERE username = %s", (username,)
            ).fetchone()
    except Exception as exc:      # noqa: BLE001
        _last_error = f"อ่านบัญชีจากฐานข้อมูลไม่ได้: {exc}"
        log.critical("อ่านบัญชี %s ไม่ได้: %s", username, exc)
        return None

    _last_error = ""
    return _row_to_record(row) if row else None


def upsert(username: str, record: dict) -> None:
    """สร้างหรือแก้บัญชีเดียว — ไม่แตะบัญชีคนอื่น"""
    global _last_error

    if not db.enabled():
        users = _json_load_all()
        users[username] = record
        _json_save_all(users)
        return

    try:
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (username, password, role, created_at, last_login)
                VALUES (%s, %s, %s,
                        COALESCE(%s::timestamptz, now()),
                        %s::timestamptz)
                ON CONFLICT (username) DO UPDATE SET
                    password   = EXCLUDED.password,
                    role       = EXCLUDED.role,
                    last_login = EXCLUDED.last_login
                """,
                (
                    username,
                    record.get("password", ""),
                    record.get("role", "member"),
                    record.get("created_at") or None,
                    record.get("last_login") or None,
                ),
            )
    except Exception as exc:      # noqa: BLE001
        raise _pg_fail(exc, f"บันทึกบัญชี {username}") from exc

    _last_error = ""


def touch_login(username: str, when: str) -> None:
    """อัปเดตเวลาเข้าระบบล่าสุด — แถวเดียว ไม่ต้องอ่านทั้งตาราง"""
    if not db.enabled():
        users = _json_load_all()
        if username in users:
            users[username]["last_login"] = when
            _json_save_all(users)
        return

    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET last_login = %s::timestamptz WHERE username = %s",
            (when, username),
        )


def delete(username: str) -> None:
    global _last_error

    if not db.enabled():
        users = _json_load_all()
        users.pop(username, None)
        _json_save_all(users)
        return

    try:
        with db.connect() as conn:
            conn.execute("DELETE FROM users WHERE username = %s", (username,))
    except Exception as exc:      # noqa: BLE001
        raise _pg_fail(exc, f"ลบบัญชี {username}") from exc

    _last_error = ""


def count() -> int:
    global _last_error

    if not db.enabled():
        return len(_json_load_all())

    try:
        with db.connect() as conn:
            row = conn.execute("SELECT count(*) FROM users").fetchone()
    except Exception as exc:      # noqa: BLE001
        _last_error = f"นับบัญชีจากฐานข้อมูลไม่ได้: {exc}"
        log.critical("นับตาราง users ไม่ได้: %s", exc)
        return 0

    _last_error = ""
    return int(row[0]) if row else 0


def admins() -> list[str]:
    """ชื่อผู้ดูแลระบบทั้งหมด — ใช้กันการลบ/ลดสิทธิ์ admin คนสุดท้าย"""
    global _last_error

    if not db.enabled():
        return [n for n, r in _json_load_all().items() if r.get("role") == "admin"]

    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT username FROM users WHERE role = 'admin' ORDER BY username"
            ).fetchall()
    except Exception as exc:      # noqa: BLE001
        _last_error = f"อ่านรายชื่อผู้ดูแลจากฐานข้อมูลไม่ได้: {exc}"
        log.critical("อ่าน admin list ไม่ได้: %s", exc)
        return []

    _last_error = ""
    return [row[0] for row in rows]


def save_all(users: dict[str, dict]) -> None:
    """เขียนทับทั้งชุด — ใช้เฉพาะสคริปต์ย้ายข้อมูล ไม่ใช้ในเส้นทางปกติ"""
    if not db.enabled():
        _json_save_all(users)
        return
    for username, record in users.items():
        upsert(username, record)


def load_json_file() -> dict[str, dict]:
    """อ่าน storage/users.json ตรง ๆ ไม่สนว่าตั้ง DATABASE_URL ไว้หรือไม่ (ใช้ตอน migrate)"""
    return _json_load_all()
