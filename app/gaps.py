"""บันทึกคำถามที่ระบบตอบได้ไม่ดี — ใช้รู้ว่ายังขาดเอกสารเรื่องอะไร

ตอบว่า "ยังไม่พบข้อมูลนี้ในเอกสารที่มีอยู่" คือสัญญาณว่าคลังความรู้มีรู
ถ้าไม่เก็บไว้ รูนั้นจะไม่มีใครรู้จนกว่าจะมีคนบ่น

เก็บลงตาราง unanswered ใน Postgres — ไม่มี DATABASE_URL ก็ข้ามไปเงียบ ๆ
(ฟีเจอร์นี้ต้องไม่ทำให้การถาม-ตอบพังไม่ว่ากรณีใด)
"""

from __future__ import annotations

import logging

from . import config, db

log = logging.getLogger(__name__)

# ยาวกว่านี้เป็นการวางข้อความยาว ๆ มามากกว่าคำถามจริง ตัดทิ้งเพื่อไม่ให้ตารางบวม
_MAX_QUESTION = 500


def should_log(result: dict) -> bool:
    """ตอบไม่ได้ หรือตอบได้แต่หลักฐานอ่อน = ควรบันทึกไว้ดู"""
    status = result.get("status", "")
    if status in ("no_index", "retrieval_only"):
        return True
    return float(result.get("top_score") or 0.0) < config.MIN_SCORE


def record(question: str, result: dict, username: str | None) -> None:
    if not db.enabled() or not should_log(result):
        return

    try:
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO unanswered (username, question, status, top_score)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    username,
                    question.strip()[:_MAX_QUESTION],
                    result.get("status", ""),
                    float(result.get("top_score") or 0.0),
                ),
            )
    except Exception as exc:      # noqa: BLE001 — บันทึกไม่ได้ ห้ามทำให้คำตอบหาย
        log.warning("บันทึกคำถามที่ตอบไม่ได้ล้มเหลว: %s", exc)


def top(limit: int = 50) -> list[dict]:
    """คำถามที่ตอบไม่ได้ จัดกลุ่มตามข้อความ เรียงตามจำนวนครั้ง"""
    if not db.enabled():
        return []

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT question,
                   count(*)          AS times,
                   max(asked_at)     AS last_asked,
                   round(avg(top_score)::numeric, 4) AS avg_score
            FROM unanswered
            GROUP BY question
            ORDER BY times DESC, last_asked DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()

    return [
        {
            "question": row[0],
            "times": int(row[1]),
            "last_asked": row[2].isoformat(timespec="seconds") if row[2] else "",
            "avg_score": float(row[3]) if row[3] is not None else 0.0,
        }
        for row in rows
    ]
