"""สร้าง index จากบรรทัดคำสั่ง

    python scripts/ingest.py
    python scripts/ingest.py --data-dir "D:/เอกสาร TA"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config                     # noqa: E402
from app.rag import pipeline               # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="สร้าง RAG index สำหรับ TA Assistant")
    parser.add_argument(
        "--data-dir", type=Path, default=config.DATA_DIR,
        help="โฟลเดอร์เอกสารต้นทาง (ค่าเริ่มต้น: ./data)",
    )
    args = parser.parse_args()

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
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
