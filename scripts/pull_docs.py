"""ดึงเอกสารที่แอดมินอัปโหลดผ่านหน้าเว็บ (พักไว้บน Blob) ลงมาที่ data/

บน Vercel เขียนดิสก์ไม่ได้ ไฟล์ที่อัปผ่านหน้าเว็บจึงไปพักบน Blob ใต้ docs/
สคริปต์นี้คือขาที่ดึงกลับมาให้ ingest ในเครื่องเห็น

    python scripts/pull_docs.py
    python scripts/pull_docs.py --data-dir "D:/เอกสาร TA"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import blobstore, config          # noqa: E402

_DOCS_PREFIX = "docs/"


def main() -> int:
    parser = argparse.ArgumentParser(description="ดึงเอกสารจาก Vercel Blob ลง data/")
    parser.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    parser.add_argument(
        "--overwrite", action="store_true",
        help="ทับไฟล์ที่มีอยู่แล้ว (ค่าเริ่มต้นคือข้าม)",
    )
    args = parser.parse_args()

    if not blobstore.enabled():
        print("✗ ยังไม่ได้ตั้ง BLOB_READ_WRITE_TOKEN ใน .env")
        return 1

    args.data_dir.mkdir(parents=True, exist_ok=True)
    items = [b for b in blobstore.list_keys(_DOCS_PREFIX) if b["key"] != _DOCS_PREFIX]

    if not items:
        print("ยังไม่มีเอกสารบน Blob — อัปโหลดผ่านหน้าเว็บก่อนนะครับ")
        return 0

    print(f"📥 ปลายทาง : {args.data_dir}")
    print("-" * 58)

    downloaded = 0
    for item in items:
        name = item["key"][len(_DOCS_PREFIX):]
        # กัน path traversal จากชื่อ blob (ฝั่งอัปโหลดกรองแล้ว แต่กันไว้อีกชั้น)
        name = name.replace("\\", "/").split("/")[-1]
        if not name:
            continue

        target = args.data_dir / name
        if target.exists() and not args.overwrite:
            print(f"  ⏭️  {name:<45} มีอยู่แล้ว (ใช้ --overwrite เพื่อทับ)")
            continue

        data = blobstore.get(item["key"])
        if data is None:
            print(f"  ❌ {name:<45} ดาวน์โหลดไม่ได้")
            continue

        target.write_bytes(data)
        downloaded += 1
        print(f"  ✅ {name:<45} {len(data) / 1024:.1f} KB")

    print("-" * 58)
    print(f"✨ ดึงมา {downloaded} ไฟล์ — ขั้นถัดไป: python scripts/ingest.py --push")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
