"""คลังเอกสารต้นฉบับ — ที่เดียวที่รู้ว่า "ไฟล์ความรู้" ถูกเก็บไว้ตรงไหน

index เก็บแต่ข้อความที่ตัดเป็น chunk แล้ว ไม่มีไฟล์จริง การจะแก้เอกสารสักไฟล์
จึงต้องหาไฟล์ต้นฉบับให้เจอก่อน ซึ่งอยู่คนละที่กันตามสภาพแวดล้อม

    รันในเครื่อง (เขียนดิสก์ได้)      -> data/
    บน Vercel (READ_ONLY_FS + Blob)  -> objectstore ใต้ docs/

โมดูลนี้ห่อความต่างนั้นไว้ ให้หน้าเว็บเรียกใช้แบบเดียวกันทั้งสองที่
"""

from __future__ import annotations

import logging
from datetime import datetime

from . import config, objectstore

log = logging.getLogger(__name__)

# โฟลเดอร์ใน objectstore ที่พักเอกสารต้นฉบับ (คู่กับ index ที่อยู่ระดับบนสุด)
DOCS_PREFIX = "docs/"


class DocError(Exception):
    """ปัญหาที่ผู้ใช้แก้เองได้ — ข้อความพร้อมแสดงบนหน้าเว็บ"""


# --------------------------------------------------------------------------- #
def use_remote() -> bool:
    """เก็บเอกสารไว้ที่ objectstore แทนดิสก์หรือไม่

    เกณฑ์เดียวกับตอนอัปโหลดและตอน ingest — ถ้าคิดคนละแบบ ไฟล์จะไปโผล่คนละที่
    กับที่ระบบตามหา แล้วกลายเป็น "อัปโหลดแล้วแต่ไม่เห็นไฟล์"
    """
    return config.READ_ONLY_FS and objectstore.enabled()


def backend() -> str:
    return objectstore.backend() if use_remote() else "disk"


def writable() -> bool:
    """แก้ไขคลังเอกสารได้ไหม — เขียนดิสก์ไม่ได้และไม่มีที่เก็บถาวร = ทำอะไรไม่ได้เลย"""
    return objectstore.enabled() or not config.READ_ONLY_FS


def safe_name(raw: str) -> str:
    """ตัดให้เหลือแค่ชื่อไฟล์ กันการอ้างไปนอกโฟลเดอร์ที่ตั้งใจ"""
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", ".."):
        raise DocError("ชื่อไฟล์ไม่ถูกต้องค่ะ")
    return name


def check_supported(name: str) -> str:
    """คืนนามสกุลถ้ารองรับ ไม่งั้นโยน DocError พร้อมบอกว่ารองรับอะไรบ้าง"""
    suffix = ("." + name.rsplit(".", 1)[-1]).lower() if "." in name else ""
    if suffix not in config.SUPPORTED_EXTENSIONS:
        raise DocError(
            f"ไม่รองรับนามสกุล {suffix or '(ไม่มีนามสกุล)'} — "
            f"รองรับ {', '.join(sorted(config.SUPPORTED_EXTENSIONS))}"
        )
    return suffix


# --------------------------------------------------------------------------- #
def list_documents() -> list[dict]:
    """รายชื่อเอกสารต้นฉบับทั้งหมด เรียงตามชื่อ — [{name, size, modified}]"""
    if use_remote():
        items = []
        for blob in objectstore.list_keys(DOCS_PREFIX):
            name = blob["key"][len(DOCS_PREFIX):]
            # อัปโหลดผ่านหน้าเว็บได้ชื่อแบน ๆ เสมอ — ที่มีชั้นโฟลเดอร์คือของแปลกปลอม
            if not name or "/" in name:
                continue
            items.append(
                {
                    "name": name,
                    "size": blob.get("size") or 0,
                    "modified": blob.get("modified", ""),
                }
            )
        return sorted(items, key=lambda d: d["name"].lower())

    items = []
    for path in sorted(config.DATA_DIR.rglob("*")):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        if path.suffix.lower() not in config.SUPPORTED_EXTENSIONS:
            continue
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(
                    timespec="seconds"
                ),
            }
        )
    return sorted(items, key=lambda d: d["name"].lower())


def names() -> set[str]:
    """ชื่อไฟล์ทั้งหมดในคลัง — ถามทีเดียวแล้วเช็คหลายไฟล์

    บน Blob การถามว่า "มีไฟล์นี้ไหม" ต้อง list ทั้งโฟลเดอร์ทุกครั้ง
    เรียก exists() ทีละไฟล์ตอนอัปโหลดหลายไฟล์จึงยิง list ซ้ำเท่าจำนวนไฟล์
    """
    return {d["name"] for d in list_documents()}


def read(name: str) -> bytes | None:
    """อ่านไฟล์ต้นฉบับกลับมา — ใช้ตอนแอดมินกดดาวน์โหลดไปแก้"""
    name = safe_name(name)
    if use_remote():
        # fresh=True เพราะ CDN ของ Blob cache ได้ถึง 60 วินาที
        # แก้ไฟล์แล้วกดดาวน์โหลดทันทีต้องได้ของที่เพิ่งอัป ไม่ใช่ของก่อนแก้
        return objectstore.get(f"{DOCS_PREFIX}{name}", fresh=True)

    path = config.DATA_DIR / name
    if not path.is_file():
        return None
    return path.read_bytes()


def exists(name: str) -> bool:
    name = safe_name(name)
    if use_remote():
        return name in names()
    return (config.DATA_DIR / name).is_file()


def save(name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    """บันทึกเอกสาร (ชื่อซ้ำ = เขียนทับ ซึ่งคือวิธีอัปเดตเอกสารที่แก้แล้ว)"""
    name = safe_name(name)
    check_supported(name)

    if not writable():
        raise DocError(
            "เซิร์ฟเวอร์เขียนไฟล์ไม่ได้ และยังไม่มีที่เก็บถาวรค่ะ — "
            "ต้องตั้ง DATABASE_URL หรือ BLOB_READ_WRITE_TOKEN อย่างน้อยหนึ่งอย่างก่อน"
        )

    if use_remote():
        objectstore.put(f"{DOCS_PREFIX}{name}", data, content_type)
        return

    (config.DATA_DIR / name).write_bytes(data)


def delete(name: str) -> bool:
    """ลบเอกสารต้นฉบับ คืน False ถ้าไม่มีไฟล์นั้นอยู่แล้ว"""
    name = safe_name(name)

    if not writable():
        raise DocError("เซิร์ฟเวอร์เขียนไฟล์ไม่ได้ และยังไม่มีที่เก็บถาวรค่ะ")

    if use_remote():
        if not exists(name):
            return False
        objectstore.delete(f"{DOCS_PREFIX}{name}")
        return True

    path = config.DATA_DIR / name
    if not path.is_file():
        return False
    path.unlink()
    return True
