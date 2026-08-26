"""เรียก Gemini generateContent เพื่อสรุปคำตอบจาก context ที่ค้นมาได้"""

from __future__ import annotations

import json
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
_MAX_RETRY_WAIT = 30.0   # ถ้า Gemini บอกให้รอนานกว่านี้ ไม่คุ้มให้ผู้ใช้นั่งรอ

# เวลาน้อยกว่านี้ไม่พอให้ Gemini ตอบจบ (วัดจริงได้ ~9-13 วินาที) ยิงไปก็เสียเปล่า
_MIN_CALL_SECONDS = 8.0

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
2. ถ้า context ไม่มีคำตอบ ห้ามเดา ให้ตอบว่า "เรื่องนี้รบกวนสอบถามเจ้าหน้าที่โดยตรงนะครับ"
   แล้วระบุว่าควรถามใคร (วิทยากร ผู้ประสานงาน หรือพี่ทีน ตามความเหมาะสมของเรื่อง)
   ห้ามใช้คำว่า "ไม่พบข้อมูล" "ไม่มีข้อมูล" หรือ "เอกสารไม่ได้ระบุ" เพราะฟังดูเป็นทางตัน
   ให้ชี้ทางไปหาคนที่ตอบได้เสมอ และถ้ามีข้อมูลใกล้เคียงที่เป็นประโยชน์ ให้เสริมให้ด้วย
3. ตอบเป็นภาษาไทย น้ำเสียงเป็นมิตร กระชับ เหมือนรุ่นพี่อธิบายให้รุ่นน้องฟัง
4. ถ้าเป็นขั้นตอนการทำงาน ให้เรียงเป็นข้อ 1. 2. 3.
5. ตัวเลขเกณฑ์ (เช่น 80% ของเวลาอบรม, Workshop 70%), ชั่วโมง-นาที, ลิงก์ และวันที่
   ต้องคงไว้ตรงตามเอกสารเป๊ะ ๆ ห้ามปัดเศษหรือประมาณ
6. ถ้าคำถามเกี่ยวกับข้อมูลส่วนบุคคลของผู้เข้าอบรม (ชื่อ ผลการประเมิน เหตุผลที่ไม่ผ่าน)
   ให้ตอบเป็นหลักเกณฑ์และวิธีปฏิบัติ ไม่ต้องระบุชื่อบุคคลออกมา
   ถ้าใน context มี [ผู้เข้าอบรม] แทนชื่อ ห้ามพยายามเดาว่าเป็นใคร
7. ความยาวไม่เกิน 250 คำ เว้นแต่คำถามต้องการรายละเอียดขั้นตอนจริง ๆ"""

# ต่อท้าย prompt เมื่อไม่แสดงการ์ดแหล่งอ้างอิง — เลข [1] [2] จะกลายเป็นตัวเลขลอย
# ที่ผู้ใช้กดดูอะไรไม่ได้ ทำให้คำตอบดูรกและน่าสับสน
_NO_CITATION_RULE = """

เพิ่มเติมที่สำคัญ: ห้ามใส่หมายเลขอ้างอิงแบบ [1] [2] และห้ามเอ่ยชื่อไฟล์เอกสารในคำตอบ
เพราะหน้าเว็บไม่ได้แสดงรายการแหล่งอ้างอิงให้ผู้ใช้กดดู
ให้เขียนเป็นคำตอบที่อ่านจบได้ในตัวเอง"""

_CITATION_RULE = """

เพิ่มเติม: อ้างอิงแหล่งที่มาท้ายประโยคที่เกี่ยวข้องด้วยรูปแบบ [1] [2]
ตามหมายเลขที่ระบุใน context"""


def _system_prompt() -> str:
    """prompt เต็ม — กติกาการอ้างอิงเปลี่ยนตามว่าหน้าเว็บแสดงแหล่งอ้างอิงหรือไม่"""
    return SYSTEM_PROMPT + (
        _CITATION_RULE if config.SHOW_SOURCES else _NO_CITATION_RULE
    )


class LLMError(RuntimeError):
    """ข้อผิดพลาดจากการเรียก LLM

    friendly = ข้อความภาษาไทยที่แสดงให้ผู้ใช้เห็นได้ (ไม่ใช่ JSON ดิบ)
    """

    def __init__(self, detail: str, friendly: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.friendly = friendly or "ระบบตอบคำถามขัดข้องชั่วคราว รบกวนลองใหม่อีกครั้งนะครับ"


def _friendly_message(status: int, body: str) -> str:
    """แปลง error ของ Gemini เป็นข้อความที่ TA อ่านเข้าใจ"""
    if status == 429:
        return (
            "ตอนนี้ระบบใช้โควตาการตอบคำถามครบตามที่กำหนดแล้วครับ "
            "รบกวนลองใหม่อีกครั้งในภายหลัง หรือสอบถามเจ้าหน้าที่โดยตรงได้เลยนะครับ"
        )
    if status in (500, 502, 503, 504):
        return (
            "ระบบตอบคำถามมีผู้ใช้งานหนาแน่นอยู่ครับ รบกวนลองถามใหม่อีกครั้งในอีกสักครู่ "
            "หรือถ้าเร่งด่วนสอบถามเจ้าหน้าที่โดยตรงได้เลยนะครับ"
        )
    if status in (401, 403):
        return "การเชื่อมต่อระบบ AI มีปัญหาด้านสิทธิ์การใช้งาน รบกวนแจ้งผู้ดูแลระบบนะครับ"
    if status == 404:
        return "ตั้งค่าโมเดล AI ไม่ถูกต้อง รบกวนแจ้งผู้ดูแลระบบนะครับ"
    return "ระบบตอบคำถามขัดข้องชั่วคราว รบกวนลองใหม่อีกครั้ง หรือสอบถามเจ้าหน้าที่ได้เลยนะครับ"


def _retry_after(body: str) -> float | None:
    """Gemini ส่ง RetryInfo.retryDelay มาบอกว่าควรรอกี่วินาที — เชื่อค่านั้นดีกว่าเดาเอง"""
    try:
        details = json.loads(body).get("error", {}).get("details", [])
    except (json.JSONDecodeError, AttributeError):
        return None

    for item in details:
        if not isinstance(item, dict):
            continue
        delay = item.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                continue
    return None


_TIMEOUT_FRIENDLY = (
    "ระบบใช้เวลาประมวลผลนานเกินกำหนดครับ รบกวนถามใหม่อีกครั้ง "
    "หรือถามให้สั้นลง — ถ้าเร่งด่วนสอบถามเจ้าหน้าที่โดยตรงได้เลยนะครับ"
)


def _post_with_retry(url: str, payload: dict, deadline: float | None = None) -> dict:
    """ยิง request พร้อม retry — แต่ไม่ยิงเกินเวลาที่เหลือ (deadline)

    บน serverless ถ้าใช้เวลาเกินเพดาน ฟังก์ชันถูกตัดทิ้งกลางทาง ผู้ใช้ได้ 504 เปล่า ๆ
    การยอมแพ้ก่อนแล้วคืนข้อความจากเอกสารให้อ่าน มีประโยชน์กว่ามาก
    """
    headers = {
        "x-goog-api-key": config.GEMINI_API_KEY,
        "Content-Type": "application/json",
    }
    last_detail = "เรียก Gemini ไม่สำเร็จ"
    last_friendly: str | None = None

    def remaining() -> float:
        return float("inf") if deadline is None else deadline - time.monotonic()

    with httpx.Client(timeout=_TIMEOUT) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            left = remaining()
            if left <= _MIN_CALL_SECONDS:
                log.warning("เวลาเหลือ %.1f วินาที ไม่พอเรียก Gemini — ยอมแพ้", left)
                raise LLMError(
                    f"หมดงบเวลาก่อนเรียก Gemini (เหลือ {left:.1f} วินาที)",
                    _TIMEOUT_FRIENDLY,
                )

            try:
                # ไม่ให้ request เดียวกินเวลาเกินที่เหลือ
                resp = client.post(
                    url, headers=headers, json=payload,
                    timeout=httpx.Timeout(min(left, _TIMEOUT.read or 90.0), connect=10.0),
                )
            except httpx.RequestError as exc:
                last_detail = f"เชื่อมต่อ Gemini ไม่ได้: {exc}"
                last_friendly = (
                    "เชื่อมต่อระบบ AI ไม่ได้ รบกวนตรวจอินเทอร์เน็ตแล้วลองใหม่นะครับ"
                )
                if attempt == _MAX_ATTEMPTS:
                    break
                wait = _BACKOFF_BASE ** attempt
            else:
                if resp.status_code < 400:
                    return resp.json()

                last_detail = f"Gemini ตอบกลับ {resp.status_code}: {resp.text[:300]}"
                last_friendly = _friendly_message(resp.status_code, resp.text)

                # 4xx อื่น ๆ (key ผิด, โมเดลไม่มี) ลองใหม่ก็ไม่ช่วย
                if resp.status_code not in _RETRY_STATUS or attempt == _MAX_ATTEMPTS:
                    break

                # Gemini บอกเองว่าควรรอกี่วินาที — เชื่อค่านั้น แต่ไม่รอนานเกินไป
                suggested = _retry_after(resp.text)
                wait = min(suggested, _MAX_RETRY_WAIT) if suggested is not None \
                    else _BACKOFF_BASE ** attempt

            # รอแล้วจะไม่เหลือเวลายิงใหม่ ก็ไม่ต้องรอ
            if wait + _MIN_CALL_SECONDS > remaining():
                log.warning(
                    "เวลาเหลือ %.1f วินาที ไม่พอรอ %.1f วินาทีแล้วลองใหม่ — ยอมแพ้",
                    remaining(), wait,
                )
                break

            log.warning(
                "Gemini ล้มเหลว (ครั้งที่ %d/%d) — รอ %.1f วินาทีแล้วลองใหม่: %s",
                attempt, _MAX_ATTEMPTS, wait, last_detail[:120],
            )
            time.sleep(wait)

    raise LLMError(last_detail, last_friendly)


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


def generate_answer(
    question: str,
    hits: list,
    history: list[dict] | None = None,
    deadline: float | None = None,
) -> str:
    if not config.has_api_key():
        raise LLMError(
            "ยังไม่ได้ตั้งค่า GEMINI_API_KEY — ระบบทำงานในโหมดค้นหาเท่านั้น",
            friendly=(
                "ระบบยังไม่ได้เชื่อมต่อ AI สำหรับสรุปคำตอบ (รบกวนแจ้งผู้ดูแลระบบ) "
                "รบกวนอ่านจากเอกสารด้านล่าง หรือสอบถามเจ้าหน้าที่โดยตรงนะครับ"
            ),
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
    generation: dict = {
        "temperature": 0.2,
        "topP": 0.9,
        # maxOutputTokens นับรวม thinking tokens ด้วย ตั้งต่ำเกินคำตอบจะถูกตัดกลางประโยค
        "maxOutputTokens": 2048,
    }
    if config.GEMINI_THINKING_BUDGET >= 0:
        generation["thinkingConfig"] = {"thinkingBudget": config.GEMINI_THINKING_BUDGET}

    payload = {
        "systemInstruction": {"parts": [{"text": _system_prompt()}]},
        "contents": contents,
        "generationConfig": generation,
    }

    data = _post_with_retry(url, payload, deadline)
    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMError("Gemini ไม่ส่งคำตอบกลับมา (อาจถูกกรองด้วย safety filter)")

    parts = candidates[0].get("content", {}).get("parts", []) or []
    answer = "".join(p.get("text", "") for p in parts).strip()
    if not answer:
        raise LLMError("Gemini ส่งคำตอบว่างเปล่ากลับมา")
    return answer
