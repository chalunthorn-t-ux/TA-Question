"""จุดเข้าสำหรับ Vercel Serverless Function

Vercel มองหาตัวแปรชื่อ `app` ที่เป็น ASGI application ในไฟล์นี้
โค้ดจริงอยู่ใน app/main.py — ไฟล์นี้แค่ทำให้ import ได้จากรากโปรเจกต์
"""

from __future__ import annotations

import sys
from pathlib import Path

# บน Vercel ตัว working directory ไม่ใช่รากโปรเจกต์ ต้องเติม path ให้ import app/ เจอ
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402  — ต้องเติม sys.path ก่อนจึง import ได้

__all__ = ["app"]
