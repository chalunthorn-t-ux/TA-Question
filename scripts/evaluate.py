"""ประเมินคุณภาพการค้นคืนของระบบ — สร้างตัวเลขสำหรับใส่รายงาน

วัดสิ่งที่วัดได้แบบภววิสัย (ไม่ต้องมีคนมาตัดสิน) คือ **การค้นคืนถูกที่หรือไม่**
เพราะถ้าดึงเอกสารผิดตั้งแต่ต้น คำตอบก็ผิดแน่นอนไม่ว่าโมเดลจะเก่งแค่ไหน

    Hit@1   ดึงเอกสารที่ถูกต้องมาเป็นอันดับ 1 ได้กี่ %
    Hit@k   เอกสารที่ถูกต้องติดอยู่ใน k อันดับแรกกี่ %
    MRR     Mean Reciprocal Rank — ถูกต้องอยู่อันดับที่เท่าไหร่โดยเฉลี่ย

และวัดอีกอย่างที่สำคัญไม่แพ้กันคือ **ระบบยอมบอกว่าไม่รู้ไหม** เมื่อถูกถามเรื่อง
นอกขอบเขต (คำถาม N**) ระบบที่เดามั่วทุกคำถามอันตรายกว่าระบบที่ตอบว่าไม่รู้

ส่วนคุณภาพ "ถ้อยคำ" ของคำตอบยังต้องให้คนตรวจ — ใช้ --answers เพื่อดึงคำตอบจริง
ออกมาเป็นตารางให้ตรวจทีละข้อ แล้วกรอกคะแนนเอง (อย่าให้ AI ตัดสินงานของ AI เอง)

    python scripts/evaluate.py
    python scripts/evaluate.py --top-k 5 --answers
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config                          # noqa: E402
from app.rag import embedder, pipeline          # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"


def load_questions(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["questions"]


# วลีที่ระบบใช้เวลาไม่มีข้อมูลจะตอบ — ใช้คัดกรองเบื้องต้นเท่านั้น
# เป็นการจับคำ ไม่ใช่การเข้าใจความหมาย ผู้ตรวจต้องอ่านคำตอบจริงยืนยันอีกที
_DECLINE_HINTS = (
    "สอบถามเจ้าหน้าที่", "สอบถามผู้ประสานงาน", "รบกวนสอบถาม",
    "ไม่มีข้อมูล", "ไม่พบข้อมูล", "ติดต่อผู้ประสานงาน",
)


def _looks_declined(answer: str) -> bool:
    return any(hint in answer for hint in _DECLINE_HINTS)


def evaluate(questions: list[dict], top_k: int, with_answers: bool) -> list[dict]:
    store = pipeline.get_store()
    rows: list[dict] = []

    for q in questions:
        vec = embedder.embed_query(q["question"], store.backend)
        hits = store.search(q["question"], vec, top_k)

        ranked = [h.chunk["source"] for h in hits]
        top_score = round(float(hits[0].score), 4) if hits else 0.0
        expected = q["expect_source"]

        # อันดับของเอกสารที่ถูกต้อง (1 = ดีที่สุด, 0 = ไม่ติดใน top-k เลย)
        rank = 0
        if expected:
            for n, source in enumerate(ranked, start=1):
                if source == expected:
                    rank = n
                    break

        # ระบบจะยอมตอบไหม — ต่ำกว่า MIN_SCORE คือ "ไม่มั่นใจพอ"
        confident = top_score >= config.MIN_SCORE

        if expected:
            passed = rank == 1
        else:
            # คำถามนอกขอบเขต: ผ่านเมื่อระบบ "ไม่เดามั่ว"
            # ตัดสินจากคำตอบสุดท้ายถ้ามี เพราะด่านที่กันได้จริงคือคำสั่งใน prompt
            # ไม่ใช่เกณฑ์คะแนน (ดูหมายเหตุใน summarise)
            passed = None

        row = {
            "id": q["id"],
            "topic": q["topic"],
            "question": q["question"],
            "expected": expected or "(ควรตอบไม่ได้)",
            "got": ranked[0] if ranked else "-",
            "rank": rank,
            "top_score": top_score,
            "confident": confident,
            "pass": passed,
        }

        if with_answers:
            result = pipeline.ask(q["question"], top_k=top_k)
            row["answer"] = result["answer"]
            row["status"] = result["status"]
            if expected is None:
                row["declined"] = _looks_declined(result["answer"])
                row["pass"] = row["declined"]

        rows.append(row)
        mark = "?" if row["pass"] is None else ("✓" if row["pass"] else "✗")
        print(f"  {mark} {q['id']}  score={top_score:<7} rank={rank}  {q['question'][:46]}")

    return rows


def summarise(rows: list[dict], top_k: int) -> dict:
    scoped = [r for r in rows if r["expected"] != "(ควรตอบไม่ได้)"]
    out_of_scope = [r for r in rows if r["expected"] == "(ควรตอบไม่ได้)"]

    def pct(n: int, total: int) -> float:
        return round(100 * n / total, 1) if total else 0.0

    hit1 = sum(1 for r in scoped if r["rank"] == 1)
    hitk = sum(1 for r in scoped if r["rank"] > 0)
    mrr = sum(1 / r["rank"] for r in scoped if r["rank"] > 0) / len(scoped) if scoped else 0.0
    # แยกสองด่านให้ชัด เพราะวัดแล้วพบว่ามันทำงานไม่เท่ากันเลย
    by_score = sum(1 for r in out_of_scope if not r["confident"])
    by_prompt = sum(1 for r in out_of_scope if r.get("declined"))
    graded = [r for r in out_of_scope if "declined" in r]

    return {
        "จำนวนคำถามในขอบเขต": len(scoped),
        "จำนวนคำถามนอกขอบเขต": len(out_of_scope),
        f"Hit@1 (ค้นถูกเป็นอันดับ 1)": f"{hit1}/{len(scoped)} = {pct(hit1, len(scoped))}%",
        f"Hit@{top_k} (ติดใน {top_k} อันดับแรก)": f"{hitk}/{len(scoped)} = {pct(hitk, len(scoped))}%",
        "MRR": round(mrr, 3),
        "คะแนนความใกล้เคียงเฉลี่ย (ในขอบเขต)":
            round(sum(r["top_score"] for r in scoped) / len(scoped), 4) if scoped else 0.0,
        "คะแนนความใกล้เคียงเฉลี่ย (นอกขอบเขต)":
            round(sum(r["top_score"] for r in out_of_scope) / len(out_of_scope), 4) if out_of_scope else 0.0,
        "ด่านที่ 1 — เกณฑ์คะแนน MIN_SCORE ปฏิเสธได้":
            f"{by_score}/{len(out_of_scope)} = {pct(by_score, len(out_of_scope))}%",
        "ด่านที่ 2 — คำสั่งใน prompt ปฏิเสธได้":
            f"{by_prompt}/{len(graded)} = {pct(by_prompt, len(graded))}%"
            if graded else "ยังไม่ได้วัด (ต้องรันด้วย --answers)",
    }


def write_report(rows: list[dict], summary: dict, top_k: int, with_answers: bool) -> Path:
    store = pipeline.get_store()
    out = EVAL_DIR / "results.md"
    lines = [
        "# ผลการประเมินระบบค้นคืนความรู้",
        "",
        f"- วันที่ประเมิน: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        f"- โมเดล embedding: `{store.backend}` (มิติ {store.vectors.shape[1] if len(store.vectors) else '-'})",
        f"- ขนาดคลังความรู้: {len(store.chunks)} chunk จาก {len(store.sources())} เอกสาร",
        f"- ค่า top_k = {top_k}, เกณฑ์ความมั่นใจขั้นต่ำ (MIN_SCORE) = {config.MIN_SCORE}",
        "",
        "## สรุปผล",
        "",
        "| ตัวชี้วัด | ค่าที่วัดได้ |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in summary.items()]

    lines += [
        "",
        "## ผลรายข้อ",
        "",
        "| รหัส | คำถาม | เอกสารที่ควรค้นเจอ | อันดับที่พบ | คะแนน | ผล |",
        "|---|---|---|:---:|---:|:---:|",
    ]
    for r in rows:
        expected = r["expected"]
        expected = expected[:34] + "…" if len(expected) > 35 else expected
        rank = r["rank"] if r["rank"] else "—"
        lines.append(
            f"| {r['id']} | {r['question']} | {expected} | {rank} | {r['top_score']} | "
            f"{'—' if r['pass'] is None else ('ผ่าน' if r['pass'] else 'ไม่ผ่าน')} |"
        )

    if with_answers:
        lines += ["", "## คำตอบที่ระบบสร้าง (ให้ผู้ตรวจให้คะแนนเอง)", ""]
        for r in rows:
            lines += [
                f"### {r['id']} — {r['question']}",
                "",
                f"> {r.get('answer', '').strip()}",
                "",
                f"*สถานะ: `{r.get('status', '-')}` · คะแนนค้นคืน: {r['top_score']}*",
                "",
                "ความถูกต้อง (ผู้ตรวจกรอก): ☐ ถูก ☐ ถูกบางส่วน ☐ ผิด ☐ ตอบไม่ได้",
                "",
            ]

    out.write_text("\n".join(lines), encoding="utf-8")

    csv_path = EVAL_DIR / "results.csv"
    # คำถามนอกขอบเขตมีคอลัมน์ declined เพิ่มมา ในขอบเขตไม่มี
    # ถ้าเอา key ของแถวแรกมาใช้ตรง ๆ จะพังตอนเจอแถวที่มีคอลัมน์เกิน
    fieldnames = list(dict.fromkeys(k for r in rows for k in r))
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="ประเมินคุณภาพการค้นคืนของระบบ")
    parser.add_argument("--questions", type=Path, default=EVAL_DIR / "questions.json")
    parser.add_argument("--top-k", type=int, default=config.TOP_K)
    parser.add_argument(
        "--answers", action="store_true",
        help="เรียก Gemini สร้างคำตอบจริงด้วย เพื่อเอาไปให้คนตรวจ (ช้าและใช้โควตา)",
    )
    args = parser.parse_args()

    if not pipeline.load_index():
        print("✗ ยังไม่มี index — รัน python scripts/ingest.py ก่อนนะครับ")
        return 1

    store = pipeline.get_store()
    print(f"📚 คลังความรู้: {len(store.chunks)} chunk จาก {len(store.sources())} เอกสาร")
    print(f"🔍 ประเมินที่ top_k={args.top_k}\n")

    questions = load_questions(args.questions)
    rows = evaluate(questions, args.top_k, args.answers)
    summary = summarise(rows, args.top_k)

    print("\n" + "=" * 58)
    for key, value in summary.items():
        print(f"  {key:<38} {value}")
    print("=" * 58)

    out = write_report(rows, summary, args.top_k, args.answers)
    print(f"\n📄 รายงานผล : {out}")
    print(f"📄 ตารางดิบ : {out.with_name('results.csv')}  (เปิดด้วย Excel ได้)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
