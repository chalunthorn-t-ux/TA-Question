"""ลบชื่อบุคคลออกจากข้อความก่อนเข้า index

เอกสารสรุปวุฒิบัตรมีชื่อ-นามสกุลจริงของผู้เข้าอบรม พร้อมเหตุผลที่ไม่ผ่านเกณฑ์
ซึ่งเป็นข้อมูลส่วนบุคคลที่ไม่ควรอยู่ในระบบที่เปิดให้คนทั่วไปถาม

หลักการ: เก็บ "วิธีปฏิบัติ" ไว้ครบ แต่แทนชื่อคนด้วยตัวแทน
    เดิม  คุณสมชาย ใจดี - ไม่ได้รับเนื่องจากขาดเรียน Day 15
    ใหม่  [ผู้เข้าอบรม] - ไม่ได้รับเนื่องจากขาดเรียน Day 15
(ตัวอย่างข้างบนใช้ชื่อสมมติ ห้ามใส่ชื่อจริงลงในโค้ดหรือคอมเมนต์)

ชื่อเจ้าหน้าที่ที่ TA ต้องติดต่อ (เช่น พี่ทีน) ต้องคงไว้ ไม่งั้นระบบจะตอบไม่ได้
ว่าควรไปหาใคร — จึงไม่แตะคำที่ขึ้นต้นด้วย "พี่" และมี allowlist กำกับอีกชั้น
"""

from __future__ import annotations

import logging
import re

from .. import config

log = logging.getLogger(__name__)

PLACEHOLDER = "[ผู้เข้าอบรม]"

# คำนำหน้าที่ใช้เรียกผู้เข้าอบรม — ไม่รวม "พี่" ซึ่งใช้เรียกเจ้าหน้าที่
_TITLES = r"(?:คุณ|นาย|นางสาว|นาง|ด\.?ญ\.?|ด\.?ช\.?)"

# "คุณ" ยังใช้เป็นสรรพนามได้ (คุณสามารถ..., คุณต้อง...) ห้ามนับเป็นชื่อคน
_NOT_NAMES = {
    "สามารถ", "ต้อง", "จะ", "ได้", "ควร", "อาจ", "กรุณา", "โปรด", "และ", "หรือ",
    "ทำ", "มี", "เป็น", "ใช้", "ไม่", "ที่", "ซึ่ง", "ก็", "ยัง", "เคย",
    "สมบัติ",   # "คุณสมบัติ" = qualification ไม่ใช่ชื่อคน
    "ภาพ",      # "คุณภาพ" = quality
    "ค่า",      # "คุณค่า" = value
}

# ชื่อ/คำเรียกที่ต้องคงไว้ (เจ้าหน้าที่ที่ TA ต้องติดต่อ)
def _allowlist() -> set[str]:
    raw = config.REDACT_KEEP_NAMES
    return {n.strip() for n in raw.split(",") if n.strip()}


# คำนำหน้า + ชื่อ + (นามสกุล)
# ต้องมีอย่างน้อย 2 คำ (ชื่อ + นามสกุล) จึงถือว่าเป็นชื่อคนจริง
# ลดโอกาสจับผิดคำอย่าง "คุณสมบัติ" หรือ "คุณสามารถ"
_FULL_NAME = re.compile(
    rf"{_TITLES}\s*([ก-๙]{{2,}})\s+([ก-๙]{{2,}})"
)


def _replace(match: re.Match) -> str:
    first, last = match.group(1), match.group(2)

    if first in _NOT_NAMES:
        return match.group(0)          # ไม่ใช่ชื่อคน ปล่อยไว้

    keep = _allowlist()
    if first in keep or last in keep or f"{first} {last}" in keep:
        return match.group(0)          # ชื่อที่ต้องคงไว้

    return PLACEHOLDER


def redact_text(text: str) -> tuple[str, int]:
    """คืน (ข้อความที่ลบชื่อแล้ว, จำนวนที่ถูกแทน)"""
    if not config.REDACT_NAMES or not text:
        return text, 0

    count = 0

    def _sub(m: re.Match) -> str:
        nonlocal count
        out = _replace(m)
        if out == PLACEHOLDER:
            count += 1
        return out

    return _FULL_NAME.sub(_sub, text), count


def redact_sections(sections: list) -> int:
    """ลบชื่อใน RawSection ทุกก้อน (แก้ในที่) คืนจำนวนที่ถูกแทนทั้งหมด"""
    if not config.REDACT_NAMES:
        return 0

    total = 0
    for section in sections:
        cleaned, n = redact_text(section.text)
        if n:
            section.text = cleaned
            total += n
    if total:
        log.info(
            "ลบชื่อบุคคล %d จุด (คงชื่อที่อยู่ใน allowlist: %s)",
            total, ", ".join(sorted(_allowlist())) or "ไม่มี",
        )
    return total
