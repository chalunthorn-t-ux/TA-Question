"""อัป index ที่สร้างในเครื่องขึ้น Vercel Blob

เว็บบน Vercel เขียนไฟล์ไม่ได้ จึงสร้าง index เองไม่ได้ ต้อง build ที่นี่แล้วส่งขึ้นไป
เว็บจะหยิบชุดใหม่ตอน cold start ถัดไป โดยไม่ต้อง deploy ใหม่

    python scripts/push_index.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import blobstore, config          # noqa: E402
from app.rag import pipeline               # noqa: E402


def _human(size: int) -> str:
    return f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


def main() -> int:
    if not blobstore.enabled():
        print("✗ ยังไม่ได้ตั้ง BLOB_READ_WRITE_TOKEN ใน .env")
        print("  สร้าง Blob store ที่ Vercel -> Storage -> Blob แล้วก็อปโทเคนมาใส่")
        return 1

    print(f"📦 ต้นทาง : {config.INDEX_DIR}")
    print(f"🏷️  prefix : {blobstore.prefix() or '(ไม่มี)'}")
    print("-" * 58)

    try:
        uploaded = pipeline.push_index()
    except FileNotFoundError as exc:
        print(f"✗ {exc}")
        return 1
    except Exception as exc:      # noqa: BLE001
        print(f"✗ อัปโหลดไม่สำเร็จ: {exc}")
        return 1

    for item in uploaded:
        print(f"  ✅ {item['name']:<16} {_human(item['bytes']):>10}")
    print("-" * 58)
    print("✨ ส่ง index ขึ้น Blob แล้ว — เว็บจะใช้ชุดนี้ตอนตื่นครั้งถัดไป")
    print("   ตรวจได้ที่ /healthz -> checks.index_source ต้องเป็น \"blob\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
