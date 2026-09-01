"""อ่าน/เขียนไฟล์บน Vercel Blob — ที่เก็บ index และเอกสารต้นฉบับตอนขึ้น production

บน Vercel ดิสก์เขียนไม่ได้ และไฟล์ที่ commit ขึ้น repo ก็แก้ไม่ได้จนกว่าจะ deploy ใหม่
Blob จึงเป็นที่เดียวที่ index ใหม่ไปโผล่ได้โดยไม่ต้อง deploy

ตั้ง BLOB_READ_WRITE_TOKEN เมื่อไหร่ระบบจะใช้ Blob ไม่ตั้งก็อ่าน/เขียนดิสก์เหมือนเดิม
เรียก REST API ตรง ๆ ด้วย httpx (มีอยู่แล้วใน requirements) ไม่ต้องพึ่ง SDK ฝั่ง JS

⚠️ โหมดของ store (private/public) เลือกตอนสร้าง แก้ทีหลังไม่ได้ และต้องส่งให้ตรงกัน
   ตอนอัปโหลด ค่าเริ่มต้นที่นี่คือ private เพราะ index มีเนื้อหาเอกสารจริงขององค์กร
   ถ้า store เป็น public ให้ตั้ง BLOB_ACCESS=public
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

_API = "https://blob.vercel-storage.com"
_API_VERSION = "12"
_TIMEOUT = 30.0

# index เปลี่ยนบ่อย ไม่ควรให้ CDN ยึดของเก่าไว้นาน
_CACHE_MAX_AGE = "60"


def token() -> str:
    return (os.getenv("BLOB_READ_WRITE_TOKEN") or "").strip()


def prefix() -> str:
    """โฟลเดอร์นำหน้าใน Blob — ต้องตั้งให้ตรงกันทั้งฝั่ง push และฝั่งเว็บ"""
    raw = (os.getenv("INDEX_BLOB_PREFIX") or "").strip().strip("/")
    return f"{raw}/" if raw else ""


def access_mode() -> str:
    mode = (os.getenv("BLOB_ACCESS") or "").strip().lower()
    return mode if mode in ("public", "private") else "private"


def enabled() -> bool:
    return bool(token())


def _auth_headers(extra: dict | None = None) -> dict:
    headers = {
        "authorization": f"Bearer {token()}",
        "x-api-version": _API_VERSION,
    }
    if extra:
        headers.update(extra)
    return headers


# --------------------------------------------------------------------------- #
def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """อัปไฟล์ทับที่ path เดิม คืน URL ของ blob"""
    pathname = f"{prefix()}{key}"
    resp = httpx.put(
        _API,
        params={"pathname": pathname},
        content=data,
        headers=_auth_headers(
            {
                "access": access_mode(),
                "x-content-type": content_type,
                "x-cache-control-max-age": _CACHE_MAX_AGE,
                # ค่าเริ่มต้นของ Vercel คือห้ามเขียนทับ ถ้าไม่ส่งอันนี้
                # push รอบที่สองจะ error ทั้งที่เราตั้งใจให้ทับของเดิม
                "x-allow-overwrite": "1",
            }
        ),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()

    payload = resp.json()
    # อัปแล้วรู้ URL เลย ไม่ต้อง list ใหม่ — เก็บ downloadUrl เพราะเป็นตัวที่ใช้ตอนอ่าน
    _url_cache[pathname] = payload.get("downloadUrl") or payload.get("url", "")
    return payload.get("url", "")


def list_keys(subprefix: str = "") -> list[dict]:
    """รายชื่อไฟล์ใต้ prefix — คืน key ที่ตัด prefix ออกแล้ว พร้อมขนาดและ URL"""
    base = f"{prefix()}{subprefix}"
    found: list[dict] = []
    cursor = ""

    while True:
        params = {"prefix": base, "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        resp = httpx.get(_API, params=params, headers=_auth_headers(), timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()

        for blob in payload.get("blobs", []):
            pathname = blob.get("pathname", "")
            found.append(
                {
                    "key": pathname[len(prefix()):],
                    "pathname": pathname,
                    "size": blob.get("size", 0),
                    "url": blob.get("downloadUrl") or blob.get("url", ""),
                }
            )

        cursor = payload.get("cursor") or ""
        if not payload.get("hasMore") or not cursor:
            return found


# จำผลการ list ไว้ในหน่วยความจำ — cold start หนึ่งครั้งต้องหา URL หลายไฟล์
# ถ้าไม่จำ จะยิง list API ซ้ำทุกไฟล์ ซึ่งนับเป็น advanced operation ทุกครั้ง
_url_cache: dict[str, str] = {}


def _url_for(key: str, *, refresh: bool = False) -> str:
    pathname = f"{prefix()}{key}"

    if refresh or not _url_cache:
        _url_cache.clear()
        for blob in list_keys():
            _url_cache[blob["pathname"]] = blob["url"]

    url = _url_cache.get(pathname, "")
    if url or refresh:
        return url

    # ไม่เจอในของที่จำไว้ อาจเป็นไฟล์ที่เพิ่งอัปหลังจากนั้น — ถามใหม่อีกรอบ
    return _url_for(key, refresh=True)


def get(key: str) -> bytes | None:
    """ดาวน์โหลดไฟล์ คืน None ถ้าไม่มี (ไม่ใช่ error — index อาจยังไม่เคย push)

    store แบบ private อ่านได้เฉพาะคนที่ถือ token จึงต้องแนบ authorization ไปด้วย
    ส่วน store แบบ public จะไม่สนใจ header นี้ ใช้เส้นทางเดียวกันได้ทั้งคู่
    """
    url = _url_for(key)
    if not url:
        return None

    resp = httpx.get(
        url,
        headers={"authorization": f"Bearer {token()}"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def exists(key: str) -> bool:
    return bool(_url_for(key))


def delete(key: str) -> None:
    url = _url_for(key)
    if not url:
        return
    httpx.post(
        f"{_API}/delete",
        json={"urls": [url]},
        headers=_auth_headers({"content-type": "application/json"}),
        timeout=_TIMEOUT,
    ).raise_for_status()
