"""สร้าง index จากบรรทัดคำสั่ง

    python scripts/ingest.py
    python scripts/ingest.py --data-dir "D:/เอกสาร TA"
    python scripts/ingest.py --push          # สร้างแล้วส่งขึ้น Vercel Blob ให้เว็บใช้ต่อ
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import blobstore, config          # noqa: E402
from app.rag import pipeline               # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="สร้าง RAG index สำหรับ TA Assistant")
    parser.add_argument(
        "--data-dir", type=Path, default=config.DATA_DIR,
        help="โฟลเดอร์เอกสารต้นทาง (ค่าเริ่มต้น: ./data)",
    )
    parser.add_argument(
        "--push", action="store_true",
        help="อัป index ขึ้น Vercel Blob ต่อทันทีเมื่อสร้างสำเร็จ",
    )
    args = parser.parse_args()

    # เช็คโทเคนตั้งแต่ต้น ไม่ใช่หลัง ingest เสร็จ — ไม่งั้นรอ embed เป็นนาทีแล้วมาตกตอนท้าย
    if args.push and not blobstore.enabled():
        print("✗ สั่ง --push แต่ยังไม่ได้ตั้ง BLOB_READ_WRITE_TOKEN ใน .env")
        return 1

    print(f"📂 โฟลเดอร์ต้นทาง : {args.data_dir}")
    print(f"🔑 Gemini API key : {'พบ' if config.has_api_key() else 'ไม่พบ (จะใช้ embedding สำรอง)'}")
    print(f"✂️  chunk size     : {config.CHUNK_SIZE} (overlap {config.CHUNK_OVERLAP})")
    if config.REDACT_NAMES:
        print(f"🔒 ลบชื่อบุคคล    : เปิด (คงชื่อ: {config.REDACT_KEEP_NAMES})")
    else:
        print("🔒 ลบชื่อบุคคล    : ปิด")
    print("-" * 58)

    result = pipeline.ingest(args.data_dir)

    for f in result.get("files", []):
        print(f"  ✅ {f['name']:<45} {f['sections']:>4} ส่วน")
    for f in result.get("failed", []):
        print(f"  ❌ {f['name']:<45} {f['error']}")

    print("-" * 58)
    print(("✨ " if result["ok"] else "⚠️  ") + result["message"])
    if result["ok"]:
        print(f"   embedding backend : {result['backend']}")
        redacted = result.get("redacted_names", 0)
        if redacted:
            print(f"   ลบชื่อบุคคล        : {redacted} จุด -> [ผู้เข้าอบรม]")
        print(f"   บันทึกที่          : {config.INDEX_DIR}")

    if not result["ok"]:
        return 1

    if args.push:
        print("-" * 58)
        try:
            uploaded = pipeline.push_index()
        except Exception as exc:      # noqa: BLE001
            print(f"⚠️  สร้าง index สำเร็จ แต่ push ขึ้น Blob ไม่ได้: {exc}")
            print("   ลองใหม่ด้วย python scripts/push_index.py (ไม่ต้อง ingest ซ้ำ)")
            return 1
        for item in uploaded:
            print(f"  ⬆️  {item['name']:<16} {item['bytes'] / 1024:>8.1f} KB")
        print("✨ ส่งขึ้น Blob แล้ว — เว็บจะใช้ชุดนี้ตอนตื่นครั้งถัดไป")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
