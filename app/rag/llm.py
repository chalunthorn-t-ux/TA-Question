"""เรียก Gemini generateContent เพื่อสรุปคำตอบจาก context ที่ค้นมาได้"""

from __future__ import annotations

import logging

import httpx

from .. import config

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(90.0, connect=15.0)

SYSTEM_PROMPT = """คุณคือ "พี่เลี้ยง TA" ผู้ช่วยตอบคำถามสำหรับผู้ช่วยสอน (Teaching Assistant) คนใหม่

กติกาการตอบ:
1. ตอบจาก <context> ที่ให้มาเท่านั้น ห้ามเดาหรือแต่งข้อมูลขึ้นเอง
2. ถ้า context ไม่มีคำตอบ ให้บอกตรง ๆ ว่า "ยังไม่พบข้อมูลนี้ในเอกสารที่มีอยู่" แล้วแนะนำว่าควรถามใครหรือดูที่ไหนต่อ
3. ตอบเป็นภาษาไทย น้ำเสียงเป็นมิตร กระชับ เหมือนรุ่นพี่อธิบายให้รุ่นน้องฟัง
4. อ้างอิงแหล่งที่มาท้ายประโยคที่เกี่ยวข้องด้วยรูปแบบ [1] [2] ตามหมายเลขที่ระบุใน context
5. ถ้าเป็นขั้นตอนการทำงาน ให้เรียงเป็นข้อ 1. 2. 3. ถ้าเป็นวันเวลาหรือจำนวนเงิน ให้คงตัวเลขตามเอกสารเป๊ะ ๆ
6. ความยาวไม่เกิน 250 คำ เว้นแต่คำถามต้องการรายละเอียดขั้นตอนจริง ๆ"""


class LLMError(RuntimeError):
    pass


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

    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(
            url,
            headers={
                "x-goog-api-key": config.GEMINI_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if resp.status_code >= 400:
        raise LLMError(f"Gemini ตอบกลับ {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMError("Gemini ไม่ส่งคำตอบกลับมา (อาจถูกกรองด้วย safety filter)")

    parts = candidates[0].get("content", {}).get("parts", []) or []
    answer = "".join(p.get("text", "") for p in parts).strip()
    if not answer:
        raise LLMError("Gemini ส่งคำตอบว่างเปล่ากลับมา")
    return answer
