"""อ่าน/เขียนไฟล์บน Vercel Blob — ที่เก็บ index และเอกสารต้นฉบับตอนขึ้น production

บน Vercel ดิสก์เขียนไม่ได้ และไฟล์ที่ commit ขึ้น repo ก็แก้ไม่ได้จนกว่าจะ deploy ใหม่
Blob จึงเป็นที่เดียวที่ index ใหม่ไปโผล่ได้โดยไม่ต้อง deploy

ตั้ง BLOB_READ_WRITE_TOKEN เมื่อไหร่ระบบจะใช้ Blob ไม่ตั้งก็อ่าน/เขียนดิสก์เหมือนเดิม
เรียก REST API ตรง ๆ ด้วย httpx (มีอยู่แล้วใน requirements) ไม่ต้องพึ่ง SDK ฝั่ง JS

⚠️ blob ที่อัปแบบ public ใครรู้ URL ก็เปิดอ่านได้ และ index.json มีเนื้อหาเอกสารจริง
   ให้ตั้ง INDEX_BLOB_PREFIX เป็นค่าสุ่มยาว ๆ (เช่น "idx-9f3a1c7b8e/") แล้วเก็บเป็นความลับ
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

_API = "https://blob.vercel-storage.com"
_API_VERSION = "7"
_TIMEOUT = 30.0


def token() -> str:
    return (os.getenv("BLOB_READ_WRITE_TOKEN") or "").strip()


def prefix() -> str:
    """โฟลเดอร์นำหน้าใน Blob — ใส่ค่าสุ่มไว้กันคนเดา URL เจอ"""
    raw = (os.getenv("INDEX_BLOB_PREFIX") or "").strip().strip("/")
    return f"{raw}/" if raw else ""


def enabled() -> bool:
    return bool(token())


def _headers(extra: dict | None = None) -> dict:
    headers = {
        "authorization": f"Bearer {token()}",
        "x-api-version": _API_VERSION,
    }
    if extra:
        headers.update(extra)
    return headers


def _public_base() -> str:
    """เดา host สาธารณะจาก token (รูปแบบ vercel_blob_rw_<storeId>_<secret>)

    ใช้ให้ดาวน์โหลดได้โดยไม่ต้องยิง list API ก่อน = ประหยัด 1 round trip ตอน cold start
    ถ้าเดาไม่ได้จะถอยไปใช้ list API แทน
    """
    parts = token().split("_")
    if len(parts) >= 4 and parts[3]:
        return f"https://{parts[3].lower()}.public.blob.vercel-storage.com"
    return ""


# --------------------------------------------------------------------------- #
def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """อัปไฟล์ทับที่ path เดิม คืน URL สาธารณะ"""
    pathname = f"{prefix()}{key}"
    resp = httpx.put(
        f"{_API}/{pathname}",
        content=data,
        headers=_headers(
            {
                "x-content-type": content_type,
                # ปิด suffix สุ่ม ไม่งั้นอัปทีได้ path ใหม่ทุกครั้ง หาไฟล์เดิมไม่เจอ
                "x-add-random-suffix": "0",
                "x-access": "public",
                # index เปลี่ยนบ่อย ไม่ควรให้ CDN ยึดของเก่าไว้นาน
                "x-cache-control-max-age": "60",
            }
        ),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("url", "")


def _resolve_url(key: str) -> str:
    """หา URL ของไฟล์ผ่าน list API — ใช้เมื่อเดา host จาก token ไม่ได้ หรือเดาแล้วไม่เจอ"""
    pathname = f"{prefix()}{key}"
    resp = httpx.get(
        _API,
        params={"prefix": pathname, "limit": "1"},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    for blob in resp.json().get("blobs", []):
        if blob.get("pathname") == pathname:
            return blob.get("downloadUrl") or blob.get("url", "")
    return ""


def list_keys(subprefix: str = "") -> list[dict]:
    """รายชื่อไฟล์ใต้ prefix — คืน key ที่ตัด prefix ออกแล้ว พร้อมขนาดและ URL"""
    base = f"{prefix()}{subprefix}"
    found: list[dict] = []
    cursor = ""

    while True:
        params = {"prefix": base, "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        resp = httpx.get(_API, params=params, headers=_headers(), timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()

        for blob in payload.get("blobs", []):
            pathname = blob.get("pathname", "")
            found.append(
                {
                    "key": pathname[len(prefix()):],
                    "size": blob.get("size", 0),
                    "url": blob.get("downloadUrl") or blob.get("url", ""),
                }
            )

        cursor = payload.get("cursor") or ""
        if not payload.get("hasMore") or not cursor:
            return found


def get(key: str) -> bytes | None:
    """ดาวน์โหลดไฟล์ คืน None ถ้าไม่มี (ไม่ใช่ error — index อาจยังไม่เคย push)"""
    pathname = f"{prefix()}{key}"

    base = _public_base()
    if base:
        resp = httpx.get(f"{base}/{pathname}", timeout=_TIMEOUT)
        if resp.status_code == 200:
            return resp.content
        if resp.status_code != 404:
            resp.raise_for_status()

    url = _resolve_url(key)
    if not url:
        return None

    resp = httpx.get(url, timeout=_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def exists(key: str) -> bool:
    return get(key) is not None


def delete(key: str) -> None:
    httpx.post(
        f"{_API}/delete",
        json={"urls": [f"{_public_base()}/{prefix()}{key}"]},
        headers=_headers({"content-type": "application/json"}),
        timeout=_TIMEOUT,
    ).raise_for_status()
