"""ถอดข้อความจากรูปภาพที่ฝังในเอกสารด้วย Gemini Vision

เอกสารอบรมภาษาไทยมักแปะตารางหรือ screenshot เป็นรูปภาพ ซึ่งสกัดเป็นข้อความไม่ได้
โมดูลนี้ส่งรูปไปให้ Gemini ถอดเป็นตาราง Markdown แล้ว cache ผลไว้
เพื่อไม่ให้เสียค่า API ซ้ำทุกครั้งที่ ingest ใหม่
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path

import httpx

from .. import config

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
_CACHE_FILE = "vision_cache.json"

# รูปเล็กมากมักเป็นไอคอน/โลโก้ ไม่คุ้มค่า API
MIN_IMAGE_BYTES = 8_000
# กันเอกสารที่แปะรูปเป็นสิบ ๆ รูปจนค่า API พุ่ง
MAX_IMAGES_PER_FILE = 12

PROMPT = """ถอดเนื้อหาในรูปภาพนี้ออกมาเป็นข้อความภาษาไทยให้ครบถ้วนที่สุด

- ถ้าเป็นตาราง ให้เขียนเป็นตาราง Markdown และคงหัวคอลัมน์กับทุกแถวไว้ครบ
- ถ้าเป็นแผนผังหรือขั้นตอน ให้เขียนเป็นลำดับข้อ
- ถ้าเป็น screenshot ของหน้าจอ ให้อธิบายว่าเป็นหน้าอะไรและมีข้อความ/ปุ่มอะไร
- คงตัวเลข หน่วย เปอร์เซ็นต์ และชื่อเฉพาะไว้ตรงตามรูปเป๊ะ ๆ ห้ามปรับ ห้ามปัดเศษ
- ตอบเฉพาะเนื้อหาที่ถอดได้ ไม่ต้องมีคำนำหรือคำอธิบายของคุณเอง
- ถ้ารูปไม่มีข้อความที่มีความหมาย (เช่น เป็นโลโก้ เส้นคั่น หรือรูปประดับ) ตอบคำเดียวว่า: ไม่มีเนื้อหา"""


def _cache_path() -> Path:
    return config.INDEX_DIR / _CACHE_FILE


def _load_cache() -> dict[str, str]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("อ่าน vision cache ไม่ได้ — เริ่มใหม่")
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    try:
        _cache_path().write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("บันทึก vision cache ไม่ได้: %s", exc)


def _guess_mime(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "webp": "image/webp",
        "tif": "image/tiff",
        "tiff": "image/tiff",
    }.get(ext, "image/png")


def _call_gemini(data: bytes, mime: str) -> str:
    url = f"{config.GEMINI_BASE_URL}/models/{config.GEMINI_CHAT_MODEL}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
                    {"text": PROMPT},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096},
    }

    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(
            url,
            headers={
                "x-goog-api-key": config.GEMINI_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Gemini vision ตอบกลับ {resp.status_code}: {resp.text[:200]}")

    candidates = resp.json().get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", []) or []
    return "".join(p.get("text", "") for p in parts).strip()


def transcribe(images: list[tuple[str, bytes]]) -> list[tuple[str, str]]:
    """รับ [(ชื่อรูป, ไบต์)] คืน [(ชื่อรูป, ข้อความที่ถอดได้)] — ข้ามรูปที่ไม่มีเนื้อหา

    ถ้าไม่มี API key จะคืนลิสต์ว่าง (ระบบยัง ingest ข้อความปกติได้)
    """
    if not config.has_api_key() or not images:
        if images and not config.has_api_key():
            log.warning("พบรูปภาพ %d รูป แต่ยังไม่มี GEMINI_API_KEY — ข้ามการอ่านรูป", len(images))
        return []

    cache = _load_cache()
    results: list[tuple[str, str]] = []
    dirty = False

    for name, data in images[:MAX_IMAGES_PER_FILE]:
        if len(data) < MIN_IMAGE_BYTES:
            continue

        key = hashlib.sha256(data).hexdigest()
        if key in cache:
            text = cache[key]
        else:
            try:
                text = _call_gemini(data, _guess_mime(name))
            except Exception as exc:  # noqa: BLE001 — รูปเดียวพังไม่ควรล้มทั้งไฟล์
                log.warning("อ่านรูป %s ไม่ได้: %s", name, exc)
                continue
            cache[key] = text
            dirty = True
            log.info("ถอดข้อความจากรูป %s ได้ %d ตัวอักษร", name, len(text))

        if text and "ไม่มีเนื้อหา" not in text[:30]:
            results.append((name, text))

    if dirty:
        _save_cache(cache)
    return results
