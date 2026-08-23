"""ระบบสมาชิก: สมัคร เข้าสู่ระบบ และแบ่งสิทธิ์

เก็บผู้ใช้ในไฟล์ JSON ที่ storage/users.json
รหัสผ่านเก็บเป็น PBKDF2-HMAC-SHA256 พร้อม salt เฉพาะรายคน ไม่เก็บรหัสจริง

คนแรกที่สมัครจะได้สิทธิ์ admin อัตโนมัติ คนถัดไปเป็น member
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config

log = logging.getLogger(__name__)

_USERS_FILE = "users.json"

# PBKDF2 รอบสูงเพื่อให้เดารหัสแบบ brute force แพง
_ITERATIONS = 210_000
_SALT_BYTES = 16

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

MIN_USERNAME = 3
MAX_USERNAME = 32
MIN_PASSWORD = 8
MAX_PASSWORD = 128

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

# กันเดารหัสรัว ๆ — นับความล้มเหลวต่อชื่อผู้ใช้ (เก็บในหน่วยความจำ)
_MAX_FAILURES = 8
_LOCKOUT_SECONDS = 300
_failures: dict[str, list[float]] = {}


class AuthError(Exception):
    """ข้อผิดพลาดที่แสดงให้ผู้ใช้เห็นได้ตรง ๆ"""


@dataclass
class User:
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


# --------------------------------------------------------------------------- #
# เก็บ/อ่านไฟล์ผู้ใช้
# --------------------------------------------------------------------------- #
def _users_path():
    return config.INDEX_DIR / _USERS_FILE


# ไฟล์ผู้ใช้เสียหาย — เก็บสาเหตุไว้เพื่อแสดงใน /healthz
# ถ้าปล่อยให้คืนค่าว่างเงียบ ๆ อาการจะเหมือน "ยังไม่มีใครสมัคร" ซึ่งหลอกให้ไล่ผิดทาง
users_file_error: str = ""


def load_users() -> dict[str, dict]:
    global users_file_error

    path = _users_path()
    if not path.exists():
        users_file_error = ""
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        users_file_error = f"users.json ไม่ใช่ JSON ที่ถูกต้อง (บรรทัด {exc.lineno} คอลัมน์ {exc.colno}): {exc.msg}"
        log.critical(
            "ไฟล์ผู้ใช้เสียหาย -> ทุกคนล็อกอินไม่ได้! %s "
            "มักเกิดจากการแก้ไฟล์ด้วยมือแล้ววงเล็บไม่ครบ "
            "ใช้ scripts/users.py จัดการบัญชีแทนการแก้ไฟล์เอง",
            users_file_error,
        )
        return {}
    except OSError as exc:
        users_file_error = f"อ่านไฟล์ users.json ไม่ได้: {exc}"
        log.critical("อ่านไฟล์ผู้ใช้ไม่ได้ -> ทุกคนล็อกอินไม่ได้! %s", exc)
        return {}

    if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
        users_file_error = "users.json มีโครงสร้างไม่ถูกต้อง (ต้องมีคีย์ 'users' เป็นอ็อบเจกต์)"
        log.critical("โครงสร้างไฟล์ผู้ใช้ผิด -> ทุกคนล็อกอินไม่ได้! %s", users_file_error)
        return {}

    users_file_error = ""
    return data["users"]


def _save_users(users: dict[str, dict]) -> None:
    path = _users_path()
    payload = {"version": 1, "users": users}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # เขียนไฟล์ชั่วคราวแล้วเปลี่ยนชื่อ กันไฟล์เสียหายถ้าดับกลางทาง
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        # Vercel และ serverless อื่น ๆ เขียนไฟล์ไม่ได้
        raise AuthError(
            "ระบบนี้เปิดให้สมัครสมาชิกไม่ได้ (เซิร์ฟเวอร์เขียนไฟล์ไม่ได้) "
            "รบกวนติดต่อผู้ดูแลระบบเพื่อสร้างบัญชีให้นะครับ"
        ) from exc


def user_count() -> int:
    return len(load_users())


def has_any_user() -> bool:
    return user_count() > 0


def list_users() -> list[dict]:
    return [
        {
            "username": name,
            "role": rec.get("role", ROLE_MEMBER),
            "created_at": rec.get("created_at", ""),
            "last_login": rec.get("last_login", ""),
        }
        for name, rec in sorted(load_users().items())
    ]


# --------------------------------------------------------------------------- #
# รหัสผ่าน
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(expected, actual)


# --------------------------------------------------------------------------- #
# ตรวจความถูกต้องของข้อมูลที่กรอก
# --------------------------------------------------------------------------- #
def normalize_username(raw: str) -> str:
    # NFKC กันตัวอักษรหน้าตาเหมือนกันแต่ code point ต่างกัน มาสมัครซ้ำชื่อเดิม
    return unicodedata.normalize("NFKC", (raw or "").strip()).lower()


def _validate_username(username: str) -> None:
    if not (MIN_USERNAME <= len(username) <= MAX_USERNAME):
        raise AuthError(f"ชื่อผู้ใช้ต้องยาว {MIN_USERNAME}-{MAX_USERNAME} ตัวอักษร")
    if not _USERNAME_RE.match(username):
        raise AuthError("ชื่อผู้ใช้ใช้ได้เฉพาะ a-z 0-9 จุด ขีดกลาง และขีดล่าง")


def _validate_password(password: str) -> None:
    if not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        raise AuthError(f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PASSWORD} ตัวอักษร")
    if password.isdigit() or password.isalpha():
        raise AuthError("รหัสผ่านควรผสมทั้งตัวอักษรและตัวเลข เพื่อให้เดายากขึ้น")


# --------------------------------------------------------------------------- #
# สมัคร / เข้าสู่ระบบ
# --------------------------------------------------------------------------- #
def register(raw_username: str, password: str, confirm: str) -> User:
    username = normalize_username(raw_username)
    _validate_username(username)

    if password != confirm:
        raise AuthError("รหัสผ่านทั้งสองช่องไม่ตรงกัน")
    _validate_password(password)

    users = load_users()
    if username in users:
        raise AuthError("ชื่อผู้ใช้นี้มีอยู่แล้ว กรุณาใช้ชื่ออื่น")

    # คนแรกที่สมัคร = admin
    role = ROLE_ADMIN if not users else ROLE_MEMBER

    users[username] = {
        "password": hash_password(password),
        "role": role,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_login": "",
    }
    _save_users(users)
    log.info("สมัครสมาชิกใหม่: %s (สิทธิ์ %s)", username, role)
    return User(username=username, role=role)


def _is_locked(username: str) -> int:
    """คืนจำนวนวินาทีที่ต้องรอ ถ้ายังไม่ถูกล็อกคืน 0"""
    now = time.time()
    recent = [t for t in _failures.get(username, []) if now - t < _LOCKOUT_SECONDS]
    _failures[username] = recent
    if len(recent) >= _MAX_FAILURES:
        return int(_LOCKOUT_SECONDS - (now - recent[0])) + 1
    return 0


def _record_failure(username: str) -> None:
    _failures.setdefault(username, []).append(time.time())


def authenticate(raw_username: str, password: str) -> User:
    username = normalize_username(raw_username)

    wait = _is_locked(username)
    if wait:
        raise AuthError(
            f"กรอกรหัสผิดหลายครั้งเกินไป รบกวนรออีก {wait // 60 + 1} นาทีแล้วลองใหม่นะครับ"
        )

    users = load_users()
    record = users.get(username)

    # ไม่บอกว่าผิดที่ชื่อหรือรหัส เพื่อไม่ให้เดาได้ว่าชื่อไหนมีในระบบ
    # และคำนวณ hash หลอกเมื่อไม่มีผู้ใช้ ให้เวลาตอบสนองใกล้เคียงกัน
    if record is None:
        hash_password(password)
        _record_failure(username)
        raise AuthError("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")

    if not verify_password(password, record.get("password", "")):
        _record_failure(username)
        raise AuthError("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")

    _failures.pop(username, None)

    record["last_login"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        _save_users(users)
    except AuthError:
        pass      # อัปเดตเวลาล็อกอินไม่ได้ ไม่ควรกันคนเข้าระบบ

    return User(username=username, role=record.get("role", ROLE_MEMBER))


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def has_session_secret() -> bool:
    return bool(os.getenv("SESSION_SECRET", "").strip())


def session_secret() -> str:
    """คีย์เซ็นคุกกี้ — ต้องคงที่ ไม่งั้นล็อกอินแล้วเด้งกลับหน้าล็อกอินวนไป"""
    secret = os.getenv("SESSION_SECRET", "").strip()
    if secret:
        return secret

    generated = secrets.token_urlsafe(48)

    # บน serverless เขียนไฟล์ไม่ได้ คีย์จะเปลี่ยนทุกครั้งที่ instance ตื่นขึ้นมาใหม่
    # ทำให้คุกกี้ที่เพิ่งออกให้ ใช้กับ request ถัดไปไม่ได้ = ล็อกอินไม่สำเร็จตลอด
    # ต้องดังพอให้เห็นใน log ไม่ใช่ warning เบา ๆ
    if config.READ_ONLY_FS:
        log.critical(
            "ยังไม่ได้ตั้ง SESSION_SECRET และเซิร์ฟเวอร์เขียนไฟล์ไม่ได้ "
            "-> ล็อกอินจะไม่สำเร็จเลย เพราะคีย์เซ็นคุกกี้เปลี่ยนทุก request "
            "ให้ไปตั้ง environment variable ชื่อ SESSION_SECRET ที่ผู้ให้บริการ (เช่น Vercel) "
            "ให้ตรงกับค่าในไฟล์ .env ของเครื่องที่พัฒนา"
        )
        return generated

    # รันในเครื่องปกติ -> สร้างและเขียนลง .env ให้ครั้งเดียว
    env_path = config.BASE_DIR / ".env"
    try:
        with env_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# สร้างอัตโนมัติ — ห้ามเปลี่ยน ไม่งั้นทุกคนหลุดออกจากระบบ\nSESSION_SECRET={generated}\n")
        log.info("สร้าง SESSION_SECRET ใหม่และบันทึกลง .env แล้ว")
    except OSError:
        log.critical(
            "เขียน .env ไม่ได้ และยังไม่มี SESSION_SECRET "
            "-> ผู้ใช้จะล็อกอินไม่สำเร็จ ต้องตั้ง environment variable ชื่อ SESSION_SECRET เอง"
        )
    return generated
