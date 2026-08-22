"""เรียก Gemini generateContent เพื่อสรุปคำตอบจาก context ที่ค้นมาได้"""

from __future__ import annotations

import logging
import time

import httpx

from .. import config

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(90.0, connect=15.0)

# Gemini ตอบ 503 เวลาคนใช้เยอะ และ 429 เวลาชน rate limit — ทั้งสองแบบรอแล้วลองใหม่ได้
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 1.5

SYSTEM_PROMPT = """คุณคือ "พี่เลี้ยง TA" ผู้ช่วยตอบคำถามสำหรับ TA (Training Assistant) คนใหม่
ของสถาบันฝึกอบรม 9Expert Training

บริบทงานที่ควรรู้:
- TA คือผู้ช่วยดูแลคลาสอบรม ทั้งแบบ Public Training และ In-house Training
- งานหลักของ TA เช่น ให้ผู้เข้าอบรมทำแบบประเมิน (Evaluation) เปิดคลิปแนะนำสถาบัน
  ถ่ายภาพบรรยากาศในคลาส ประเมิน Attendance ร่วมกับวิทยากร และสรุปวุฒิบัตร
- บุคคลที่ TA ต้องประสานงานด้วยคือ วิทยากร ผู้ประสานงาน และพี่ทีน (กรณีเฉพาะ)
- คำที่พบบ่อย: วุฒิบัตร/Certificate, Workshop, Attendance, สิทธิ์เรียนซ้ำ, Customer Report

กติกาการตอบ:
1. ตอบจาก <context> ที่ให้มาเท่านั้น ห้ามเดาหรือแต่งข้อมูลขึ้นเอง
2. ถ้า context ไม่มีคำตอบ ให้บอกตรง ๆ ว่า "ยังไม่พบข้อมูลนี้ในเอกสารที่มีอยู่"
   แล้วแนะนำว่าควรถามใครต่อ (วิทยากร ผู้ประสานงาน หรือพี่ทีน ตามความเหมาะสม)
3. ตอบเป็นภาษาไทย น้ำเสียงเป็นมิตร กระชับ เหมือนรุ่นพี่อธิบายให้รุ่นน้องฟัง
4. อ้างอิงแหล่งที่มาท้ายประโยคที่เกี่ยวข้องด้วยรูปแบบ [1] [2] ตามหมายเลขที่ระบุใน context
5. ถ้าเป็นขั้นตอนการทำงาน ให้เรียงเป็นข้อ 1. 2. 3.
6. ตัวเลขเกณฑ์ (เช่น 80% ของเวลาอบรม, Workshop 70%), ชั่วโมง-นาที, ลิงก์ และวันที่
   ต้องคงไว้ตรงตามเอกสารเป๊ะ ๆ ห้ามปัดเศษหรือประมาณ
7. ถ้าคำถามเกี่ยวกับข้อมูลส่วนบุคคลของผู้เข้าอบรม (ชื่อ ผลการประเมิน เหตุผลที่ไม่ผ่าน)
   ให้ตอบเป็นหลักเกณฑ์และวิธีปฏิบัติ ไม่ต้องระบุชื่อบุคคลออกมา
8. ความยาวไม่เกิน 250 คำ เว้นแต่คำถามต้องการรายละเอียดขั้นตอนจริง ๆ"""


class LLMError(RuntimeError):
    pass


def _post_with_retry(url: str, payload: dict) -> dict:
    """ยิง request พร้อม exponential backoff สำหรับ error ที่ลองใหม่ได้"""
    headers = {
        "x-goog-api-key": config.GEMINI_API_KEY,
        "Content-Type": "application/json",
    }
    last_error = ""

    with httpx.Client(timeout=_TIMEOUT) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = client.post(url, headers=headers, json=payload)
            except httpx.RequestError as exc:
                last_error = f"เชื่อมต่อไม่ได้: {exc}"
                if attempt == _MAX_ATTEMPTS:
                    break
            else:
                if resp.status_code < 400:
                    return resp.json()

                last_error = f"Gemini ตอบกลับ {resp.status_code}: {resp.text[:300]}"
                # 4xx อื่น ๆ (เช่น key ผิด, โมเดลไม่มี) ลองใหม่ก็ไม่ช่วย
                if resp.status_code not in _RETRY_STATUS or attempt == _MAX_ATTEMPTS:
                    break

            wait = _BACKOFF_BASE ** attempt
            log.warning(
                "Gemini ล้มเหลว (ครั้งที่ %d/%d) — รอ %.1f วินาทีแล้วลองใหม่: %s",
                attempt, _MAX_ATTEMPTS, wait, last_error[:120],
            )
            time.sleep(wait)

    raise LLMError(last_error or "เรียก Gemini ไม่สำเร็จ")


def build_context(hits: list) -> str:
    """แปลงผลการค้นเป็นบล็อค context ที่มีหมายเลขอ้างอิง"""
    blocks = []
    for n, hit in enumerate(hits, start=1):
        chunk = hit.chunk
        header = f"[{n}] {chunk['source']}"
        if chunk.get("locator"):
            header += f" ({chunk['locator']})"
        blocks.append(f"{header}\n{chunk['text']}")
    return "\n\n---\n\n".join(blocks)


def generate_answer(question: str, hits: list, history: list[dict] | None = None) -> str:
    if not config.has_api_key():
        raise LLMError(
            "ยังไม่ได้ตั้งค่า GEMINI_API_KEY — ระบบจะแสดงเฉพาะข้อความที่ค้นเจอให้ก่อน"
        )

    context = build_context(hits) or "(ไม่พบเอกสารที่เกี่ยวข้อง)"

    contents: list[dict] = []
    for turn in (history or [])[-6:]:          # เก็บบริบทย้อนหลังพอให้ถามต่อเนื่องได้
        role = "user" if turn.get("role") == "user" else "model"
        text = str(turn.get("content", "")).strip()
        if text:
            contents.append({"role": role, "parts": [{"text": text[:2000]}]})

    contents.append(
        {
            "role": "user",
            "parts": [
                {
                    "text": (
                        f"<context>\n{context}\n</context>\n\n"
                        f"คำถาม: {question}"
                    )
                }
            ],
        }
    )

    url = f"{config.GEMINI_BASE_URL}/models/{config.GEMINI_CHAT_MODEL}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": 2048,
        },
    }

    data = _post_with_retry(url, payload)
    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMError("Gemini ไม่ส่งคำตอบกลับมา (อาจถูกกรองด้วย safety filter)")

    parts = candidates[0].get("content", {}).get("parts", []) or []
    answer = "".join(p.get("text", "") for p in parts).strip()
    if not answer:
        raise LLMError("Gemini ส่งคำตอบว่างเปล่ากลับมา")
    return answer
