"""ระบบสมาชิก: สมัคร เข้าสู่ระบบ และแบ่งสิทธิ์

ข้อมูลบัญชีอยู่ที่ไหนขึ้นกับ [app/users_repo.py](users_repo.py):
ตั้ง DATABASE_URL = เก็บลง Postgres, ไม่ตั้ง = เก็บลงไฟล์ storage/users.json เหมือนเดิม
โมดูลนี้ดูแลเฉพาะกฎ — รหัสผ่าน การตรวจข้อมูล และสิทธิ์

รหัสผ่านเก็บเป็น PBKDF2-HMAC-SHA256 พร้อม salt เฉพาะรายคน ไม่เก็บรหัสจริง

คนแรกที่สมัครจะได้สิทธิ์ admin อัตโนมัติ คนถัดไปเป็น member
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config, settings_repo, users_repo

log = logging.getLogger(__name__)

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
# เก็บ/อ่านบัญชี — ทุกอย่างวิ่งผ่าน users_repo
# --------------------------------------------------------------------------- #
def __getattr__(name: str):
    """`auth.users_file_error` ยังใช้ได้เหมือนเดิม แต่ค่ามาจาก repo

    เก็บชื่อเดิมไว้เพราะ /healthz หน้า /admin/users และ scripts/users.py อ่านตัวนี้อยู่
    """
    if name == "users_file_error":
        return users_repo.last_error()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def storage_backend() -> str:
    """'postgres' หรือ 'json' — ใช้บอกใน /healthz ว่าข้อมูลบัญชีอยู่ที่ไหน"""
    return users_repo.backend()


def load_users() -> dict[str, dict]:
    return users_repo.load_all()


def _save_users(users: dict[str, dict]) -> None:
    """เขียนทับทั้งชุด — เหลือไว้ให้สคริปต์เก่าเรียก เส้นทางปกติใช้ upsert ทีละคน"""
    try:
        users_repo.save_all(users)
    except users_repo.RepoError as exc:
        raise AuthError(str(exc)) from exc


def _upsert(username: str, record: dict) -> None:
    try:
        users_repo.upsert(username, record)
    except users_repo.RepoError as exc:
        raise AuthError(str(exc)) from exc


def user_count() -> int:
    return users_repo.count()


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
        for name, rec in sorted(users_repo.load_all().items())
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


def _new_record(password: str, role: str) -> dict:
    return {
        "password": hash_password(password),
        "role": role,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_login": "",
    }


# --------------------------------------------------------------------------- #
# สมัคร / เข้าสู่ระบบ
# --------------------------------------------------------------------------- #
def register(raw_username: str, password: str, confirm: str) -> User:
    username = normalize_username(raw_username)
    _validate_username(username)

    if password != confirm:
        raise AuthError("รหัสผ่านทั้งสองช่องไม่ตรงกัน")
    _validate_password(password)

    if users_repo.get(username) is not None:
        raise AuthError("ชื่อผู้ใช้นี้มีอยู่แล้ว กรุณาใช้ชื่ออื่น")

    # คนแรกที่สมัคร = admin
    role = ROLE_ADMIN if users_repo.count() == 0 else ROLE_MEMBER

    _upsert(username, _new_record(password, role))
    log.info("สมัครสมาชิกใหม่: %s (สิทธิ์ %s)", username, role)
    return User(username=username, role=role)


def create_user(raw_username: str, password: str, confirm: str, role: str) -> User:
    """แอดมินสร้างบัญชีให้คนอื่น — ระบุสิทธิ์ได้ตรง ๆ ไม่ใช้กฎ "คนแรกได้ admin\""""
    if role not in (ROLE_ADMIN, ROLE_MEMBER):
        raise AuthError("สิทธิ์ที่เลือกไม่ถูกต้อง")

    username = normalize_username(raw_username)
    _validate_username(username)

    if password != confirm:
        raise AuthError("รหัสผ่านทั้งสองช่องไม่ตรงกัน")
    _validate_password(password)

    existing = users_repo.get(username)
    if users_repo.last_error():
        raise AuthError(f"ชั้นเก็บข้อมูลมีปัญหา จึงยังสร้างบัญชีไม่ได้: {users_repo.last_error()}")
    if existing is not None:
        raise AuthError("ชื่อผู้ใช้นี้มีอยู่แล้ว กรุณาใช้ชื่ออื่น")

    _upsert(username, _new_record(password, role))
    log.info("แอดมินสร้างบัญชี: %s (สิทธิ์ %s)", username, role)
    return User(username=username, role=role)


def delete_user(raw_username: str, *, actor: str) -> None:
    username = normalize_username(raw_username)

    if username == normalize_username(actor):
        raise AuthError("ลบบัญชีตัวเองไม่ได้")

    if users_repo.get(username) is None:
        raise AuthError(f"ไม่พบบัญชี '{username}'")
    if users_repo.admins() == [username]:
        raise AuthError("ลบไม่ได้ เพราะเป็นผู้ดูแลระบบคนเดียวที่เหลือ")

    try:
        users_repo.delete(username)
    except users_repo.RepoError as exc:
        raise AuthError(str(exc)) from exc
    log.info("ลบบัญชี: %s (โดย %s)", username, actor)


def set_role(raw_username: str, role: str, *, actor: str) -> None:
    if role not in (ROLE_ADMIN, ROLE_MEMBER):
        raise AuthError("สิทธิ์ที่เลือกไม่ถูกต้อง")

    username = normalize_username(raw_username)
    record = users_repo.get(username)
    if record is None:
        raise AuthError(f"ไม่พบบัญชี '{username}'")

    if role == ROLE_MEMBER and users_repo.admins() == [username]:
        raise AuthError("ลดสิทธิ์ไม่ได้ เพราะจะไม่มีผู้ดูแลระบบเหลือในระบบ")

    record["role"] = role
    _upsert(username, record)
    log.info("เปลี่ยนสิทธิ์ %s เป็น %s (โดย %s)", username, role, actor)


def set_password(raw_username: str, password: str, confirm: str) -> None:
    username = normalize_username(raw_username)

    if password != confirm:
        raise AuthError("รหัสผ่านทั้งสองช่องไม่ตรงกัน")
    _validate_password(password)

    record = users_repo.get(username)
    if record is None:
        raise AuthError(f"ไม่พบบัญชี '{username}'")

    record["password"] = hash_password(password)
    _upsert(username, record)
    log.info("เปลี่ยนรหัสผ่านของ %s", username)


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
            f"กรอกรหัสผิดหลายครั้งเกินไป รบกวนรออีก {wait // 60 + 1} นาทีแล้วลองใหม่นะคะ"
        )

    record = users_repo.get(username)

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

    try:
        users_repo.touch_login(
            username, datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
    except Exception as exc:      # noqa: BLE001
        # อัปเดตเวลาล็อกอินไม่ได้ ไม่ควรกันคนเข้าระบบ
        log.warning("อัปเดตเวลาเข้าระบบล่าสุดของ %s ไม่ได้: %s", username, exc)

    return User(username=username, role=record.get("role", ROLE_MEMBER))


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def has_session_secret() -> bool:
    """มีคีย์ที่ "อยู่รอดข้าม cold start" ไหม — env หรือฐานข้อมูลก็ถือว่าใช้ได้"""
    if os.getenv("SESSION_SECRET", "").strip():
        return True
    return bool(settings_repo.get(settings_repo.KEY_SESSION_SECRET))


def session_secret() -> str:
    """คีย์เซ็นคุกกี้ — ต้องคงที่ ไม่งั้นล็อกอินแล้วเด้งกลับหน้าล็อกอินวนไป"""
    secret = os.getenv("SESSION_SECRET", "").strip()
    if secret:
        return secret

    generated = secrets.token_urlsafe(48)

    # ยังไม่ได้ตั้ง env แต่มีฐานข้อมูล -> เก็บคีย์ไว้ที่นั่น
    # ทำให้ล็อกอินใช้งานได้โดยไม่ต้องไปตั้ง env บน Vercel เลย
    # (ปัญหาเดิม: คีย์สุ่มใหม่ทุก cold start = คุกกี้ที่เพิ่งออกให้ใช้ไม่ได้)
    if settings_repo.available():
        return settings_repo.get_or_create(
            settings_repo.KEY_SESSION_SECRET, lambda: generated
        )

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
