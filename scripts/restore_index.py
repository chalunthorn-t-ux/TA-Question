"""กู้เอกสารที่หายไปจาก index โดยรวม index เก่าเข้ากับ index ปัจจุบัน

ใช้ตอนที่ไฟล์ต้นฉบับ .docx หายไปแล้ว แต่ยังมี index เก่าเก็บไว้ —
index.json มีทั้งข้อความที่ตัด chunk แล้วและเวกเตอร์ ซึ่งเป็นทุกอย่างที่ระบบใช้ตอบคำถาม
ไฟล์ต้นฉบับมีไว้แค่สร้าง index เท่านั้น จึงกู้ได้โดยไม่ต้องมีมัน

    python scripts/restore_index.py                       # ดูก่อนว่าจะเกิดอะไรขึ้น
    python scripts/restore_index.py --apply               # ทำจริง
    python scripts/restore_index.py --from D:/เก่า --apply

เอกสารชื่อซ้ำกัน: ของที่อยู่ใน index ปัจจุบันชนะ (ถือว่าใหม่กว่า)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import blobstore, config          # noqa: E402
from app.rag import pipeline               # noqa: E402
from app.rag.store import VectorStore      # noqa: E402


def _load(directory: Path) -> VectorStore:
    store = VectorStore()
    if not store.load(directory):
        raise FileNotFoundError(f"ไม่พบ index ที่ {directory}")
    return store


def _summary(store: VectorStore) -> str:
    lines = [f"{len(store.chunks)} chunk · {len(store.sources())} เอกสาร · backend={store.backend}"]
    for s in store.sources():
        lines.append(f"      - {s['name']}  ({s['chunks']} chunk)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="กู้เอกสารเก่ากลับเข้า index")
    parser.add_argument(
        "--from", dest="source_dir", type=Path, default=config.INDEX_DIR,
        help="โฟลเดอร์ที่มี index เก่า (ค่าเริ่มต้น: ./storage)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="ทำจริง (ไม่ใส่ = แสดงผลลัพธ์ที่จะเกิดขึ้นเฉย ๆ)",
    )
    args = parser.parse_args()

    # index เก่าที่จะกู้
    try:
        old = _load(args.source_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"✗ {exc}")
        return 1

    print(f"📦 index เก่า ({args.source_dir})")
    print(f"   {_summary(old)}")
    print()

    # index ปัจจุบัน — เอาจาก Blob ถ้าตั้งไว้ ไม่งั้นถือว่ายังไม่มี
    if not pipeline.load_index():
        print("📭 ยังไม่มี index ปัจจุบัน — จะกู้ของเก่าขึ้นไปทั้งชุด")
    current = pipeline.get_store()
    print(f"📥 index ปัจจุบัน (มาจาก {pipeline.index_source})")
    print(f"   {_summary(current) if current.chunks else '(ว่าง)'}")
    print()

    missing = {s["name"] for s in old.sources()} - {s["name"] for s in current.sources()}
    if not missing:
        print("✨ ไม่มีเอกสารไหนหายไป — index ปัจจุบันมีครบแล้ว ไม่ต้องกู้")
        return 0

    print(f"🔧 เอกสารที่จะกู้กลับ {len(missing)} ไฟล์:")
    for name in sorted(missing):
        print(f"      + {name}")
    print()

    if current.chunks and old.backend != current.backend:
        print(
            f"✗ รวมไม่ได้: index เก่าสร้างด้วย {old.backend} "
            f"แต่ปัจจุบันเป็น {current.backend}"
        )
        print("  เวกเตอร์ต่างโมเดลเทียบคะแนนกันไม่ได้ ต้องสร้าง index ใหม่จากไฟล์ต้นฉบับทั้งหมด")
        return 1

    if not args.apply:
        print("👀 นี่คือโหมดดูอย่างเดียว — เติม --apply เพื่อทำจริง")
        return 0

    # เอาเฉพาะ chunk ของเอกสารที่หายไป ของที่มีอยู่แล้วถือว่าใหม่กว่า ไม่แตะ
    keep = [i for i, c in enumerate(old.chunks) if c["source"] in missing]
    add_chunks = [old.chunks[i] for i in keep]
    add_vectors = old.vectors[keep]

    if current.chunks:
        current.chunks = current.chunks + add_chunks
        current.vectors = np.vstack([current.vectors, add_vectors])
    else:
        current.chunks = add_chunks
        current.vectors = add_vectors
        current.backend = old.backend
    current._build_lexical()

    # เซฟลงดิสก์ไว้เป็นสำเนาเสมอ แล้วส่งขึ้น Blob ถ้ามี
    # (ต้อง push ตรง ๆ ที่นี่ — ingest ปกติจะ push เฉพาะตอนรันบนเซิร์ฟเวอร์)
    current.save()
    print(f"💾 บันทึกลง {config.INDEX_DIR} แล้ว")

    if blobstore.enabled():
        for item in pipeline.push_index():
            print(f"   ⬆️  {item['name']:<14} {item['bytes'] / 1024:>8.1f} KB")
        print("   ✓ ส่งขึ้น Blob แล้ว ตรวจได้ที่ /healthz -> checks.index_chunks")
    else:
        print("   (ยังไม่ได้ตั้ง BLOB_READ_WRITE_TOKEN จึงไม่ได้ push ขึ้นเว็บ)")

    print(f"\n✨ กู้สำเร็จ — ตอนนี้ index มี {len(current.chunks)} chunk "
          f"จาก {len(current.sources())} เอกสาร")
    for s in current.sources():
        print(f"      - {s['name'][:65]}  ({s['chunks']} chunk)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
