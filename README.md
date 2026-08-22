# 🎓 TA Assistant — เว็บตอบคำถามสำหรับ TA ใหม่ (RAG + Gemini)

เว็บแอปถาม-ตอบที่ตอบจาก **เอกสารจริงขององค์กร** เท่านั้น พร้อมแสดงแหล่งอ้างอิงทุกคำตอบ
เพื่อให้ TA (Training Assistant) คนใหม่ตรวจสอบย้อนกลับได้ ไม่ต้องเชื่อ AI แบบตาบอด

```
คำถาม ─▶ embed ─▶ ค้น hybrid (semantic + keyword) ─▶ context 5 อันดับ ─▶ Gemini ─▶ คำตอบ + [1][2][3]
```

> **บริบทที่ prompt ตั้งไว้** — สถาบันฝึกอบรม 9Expert Training: คลาส Public / In-house,
> เกณฑ์วุฒิบัตร, การประเมิน Attendance, แบบประเมิน, สิทธิ์เรียนซ้ำ
> ถ้าจะเอาไปใช้ที่อื่น แก้ `SYSTEM_PROMPT` ใน [`app/rag/llm.py`](app/rag/llm.py)
> และปุ่มคำถามแนะนำใน [`templates/index.html`](templates/index.html)

---

## เริ่มใช้งานใน 3 ขั้น

### 1. ติดตั้ง

```bash
py -3 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. ตั้งค่า API key

```bash
Copy-Item .env.example .env
```

แล้วเปิด `.env` ใส่คีย์จาก [Google AI Studio](https://aistudio.google.com/apikey)

```ini
GEMINI_API_KEY=AIza...
```

> ถ้ายังไม่ใส่คีย์ ระบบจะยังเปิดได้ในโหมด **ค้นหาเท่านั้น** (แสดงข้อความที่ค้นเจอแต่ไม่สรุปคำตอบ)
> เหมาะกับตอนดู template และทดสอบ UI

### 3. รัน

```bash
.\run.ps1
```

เปิด <http://127.0.0.1:8000> → วางไฟล์เอกสารในกล่อง "ลากไฟล์มาวาง" → กด **สร้าง Index** → ถามได้เลย

---

## โครงสร้างโปรเจกต์

```
C:\Knowleage\
├── app/
│   ├── config.py          อ่านค่าทั้งหมดจาก .env
│   ├── main.py            FastAPI: หน้าเว็บ + REST API
│   └── rag/
│       ├── loaders.py     PDF/Word/Excel/CSV/MD → RawSection
│       ├── chunker.py     ตัด chunk แบบ recursive (รองรับภาษาไทย)
│       ├── embedder.py    Gemini embedding + fallback แบบ local
│       ├── store.py       vector store + ค้นแบบ hybrid (cosine + BM25)
│       ├── vision.py      อ่านตาราง/ภาพในเอกสารด้วย Gemini Vision (+ cache)
│       ├── llm.py         prompt + เรียก Gemini generateContent (retry อัตโนมัติ)
│       └── pipeline.py    ingest() และ ask()
├── templates/index.html   หน้าเว็บ
├── static/css|js/         ธีมและ logic ฝั่ง client
├── data/                  📥 เอกสารจริง — gitignore ไว้ ไม่ขึ้น repo
├── source-pdf/            📄 PDF สำรอง — ไม่ index (ดูหัวข้อ Word vs PDF)
├── samples/               🧪 ข้อมูลสมมติสำหรับ demo — ขึ้น repo ได้
├── storage/               📦 index + vision cache (สร้างใหม่ได้ ไม่ขึ้น repo)
├── scripts/ingest.py      สร้าง index จาก CLI
└── run.ps1                ติดตั้ง + รันในคำสั่งเดียว
```

---

## จุดออกแบบที่ควรรู้

**การค้นแบบ hybrid** — ภาษาไทยไม่มีช่องว่างระหว่างคำ การค้นด้วย embedding อย่างเดียว
มักพลาดคำเฉพาะ เช่น "ฟอร์ม TA-02" ระบบจึงผสมสองคะแนน

| ส่วน | วิธี | น้ำหนัก |
|---|---|---|
| Semantic | cosine similarity ของ Gemini embedding | `HYBRID_ALPHA` (0.7) |
| Keyword | BM25 บน character 3-gram (ไม่ต้องตัดคำ) | `1 - HYBRID_ALPHA` (0.3) |

**Excel FAQ ฉลาดขึ้นอัตโนมัติ** — ถ้าไฟล์มีคอลัมน์ชื่อ `คำถาม`/`question` และ `คำตอบ`/`answer`
loader จะจับคู่ให้ 1 แถว = 1 chunk ทำให้ค้นแม่นกว่าการโยนทั้งชีตเข้าไป

**Prompt กันการเดา** — system prompt บังคับให้ตอบจาก `<context>` เท่านั้น
ถ้าไม่พบข้อมูลต้องบอกว่า "ยังไม่พบข้อมูลนี้ในเอกสารที่มีอยู่" แล้วแนะนำว่าควรถามใครต่อ
ดูและแก้ได้ที่ [`app/rag/llm.py`](app/rag/llm.py)

**Fallback ไม่พังทั้งระบบ** — ถ้า Gemini ล่มหรือไม่มีคีย์ ระบบยังคืนข้อความที่ค้นเจอให้อ่านเอง
(`status: "retrieval_only"`) ดีกว่าขึ้นหน้า error เปล่า

---

## REST API

| Method | Path | หน้าที่ |
|---|---|---|
| `GET` | `/` | หน้าเว็บ |
| `GET` | `/api/status` | จำนวน chunk, เอกสาร, โมเดล, เวลาสร้าง index |
| `POST` | `/api/ask` | `{question, history[], top_k?}` → `{answer, sources[], status}` |
| `POST` | `/api/ingest` | สร้าง index ใหม่จากทุกไฟล์ใน `data/` |
| `POST` | `/api/upload` | อัปโหลดไฟล์เข้า `data/` (multipart) |
| `GET` | `/healthz` | health check |

เอกสาร API แบบ interactive: <http://127.0.0.1:8000/docs>

---

## ปรับแต่งใน `.env`

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `GEMINI_CHAT_MODEL` | `gemini-2.5-flash` | โมเดลสรุปคำตอบ |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | โมเดล embedding |
| `GEMINI_EMBED_DIM` | `768` | มิติเวกเตอร์ (แก้แล้วต้อง ingest ใหม่) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `900` / `150` | ขนาด chunk (ตัวอักษร) |
| `TOP_K` | `5` | จำนวน chunk ที่ส่งให้ LLM |
| `MIN_SCORE` | `0.15` | คะแนนต่ำสุดที่ถือว่าเกี่ยวข้อง |
| `HYBRID_ALPHA` | `0.7` | น้ำหนัก semantic ต่อ keyword |

⚠️ **แก้ `GEMINI_EMBED_MODEL` หรือ `GEMINI_EMBED_DIM` แล้วต้องกด "สร้าง Index" ใหม่**
เวกเตอร์คนละโมเดลเทียบคะแนนกันไม่ได้

---

## รูปแบบไฟล์ที่รองรับ

| นามสกุล | วิธีอ่าน |
|---|---|
| `.docx` | python-docx — จัดกลุ่มตาม Heading + แปลงตารางเป็น `หัวคอลัมน์: ค่า` + **อ่านรูปที่ฝังด้วย Vision** |
| `.pdf` | pypdf — แยกทีละหน้า อ้างอิงเป็น "หน้า N" + อ่านรูปในหน้าที่ข้อความน้อยกว่า 120 ตัวอักษร |
| `.xlsx` `.xls` `.csv` | openpyxl / xlrd / csv — 1 แถว = 1 chunk |
| `.md` `.txt` | ตัดตามหัวข้อ `#` |

### ⚠️ Word ดีกว่า PDF สำหรับภาษาไทย — ทดสอบแล้ว

PDF ที่สร้างจาก Word มักทำ **สระอำและวรรณยุกต์หลุด** เพราะ font encoding
ทำให้ทั้งการค้นและคำตอบเพี้ยน เทียบจากเอกสารเดียวกัน:

| รูปแบบ | ข้อความที่สกัดได้ |
|---|---|
| PDF | `ต้องท า Workshop` · `ส ำหรับ กำรท ำแบบประเมิน` · `เงื่อนไขค ำนวณเวลำอบรม` |
| Word | `ต้องทำ Workshop` · `สำหรับ การทำแบบประเมิน` · `เงื่อนไขคำนวณเวลาอบรม` |

**ถ้ามีทั้งสองรูปแบบ ให้ใส่แค่ `.docx` ใน `data/`** เก็บ PDF ไว้ที่อื่น
(โปรเจกต์นี้ใช้ `source-pdf/`) ไม่งั้นเนื้อหาจะซ้ำและกินโควตา top-k ไปเปล่า ๆ

### 👁️ อ่านตาราง/ภาพในเอกสารด้วย Gemini Vision

เอกสารอบรมมักแปะตารางเป็น **รูปภาพ** ซึ่งสกัดเป็นข้อความไม่ได้เลย
[`app/rag/vision.py`](app/rag/vision.py) จะดึงรูปออกมาส่งให้ Gemini ถอดเป็นตาราง Markdown ตอน ingest

ตัวอย่างจริงในโปรเจกต์นี้ — `เงื่อนไขคำนวณเวลาอบรม.docx` มีข้อความแค่ 76 ตัวอักษร
แต่ตารางเกณฑ์ 80% ทั้งตารางเป็น PNG ขนาด 122 KB ถ้าไม่มี Vision ระบบจะตอบคำถาม
"อบรม 3 วัน ขาดได้กี่ชั่วโมง" ไม่ได้เลย

- ผลการถอดถูก **cache** ไว้ที่ `storage/vision_cache.json` (key = sha256 ของรูป) ingest ซ้ำไม่เสียเงินอีก
- ข้ามรูปเล็กกว่า 8 KB (ไอคอน/โลโก้) และจำกัดไม่เกิน 12 รูปต่อไฟล์ กันค่า API พุ่ง
- ไม่มี API key ก็ยัง ingest ข้อความปกติได้ แค่ข้ามรูป

---

## แก้ปัญหาที่พบบ่อย

| อาการ | สาเหตุ / ทางแก้ |
|---|---|
| แจ้งเตือน "ใช้ embedding สำรอง (hashing)" | ยังไม่ใส่ `GEMINI_API_KEY` หรือคีย์ผิด — คุณภาพการค้นจะต่ำกว่าปกติ |
| ตอบว่า "ยังไม่มีข้อมูลในระบบ" | ยังไม่ได้กด **สร้าง Index** หลังอัปโหลดไฟล์ |
| `index เสียหาย: จำนวน chunk ไม่ตรง` | ลบโฟลเดอร์ `storage/` แล้วสร้าง index ใหม่ |
| ค้นไม่เจอทั้งที่มีข้อมูล | ลดค่า `MIN_SCORE` หรือลด `HYBRID_ALPHA` เป็น `0.5` เพื่อให้ keyword มีน้ำหนักขึ้น |
| PDF อ่านได้ 0 ส่วน | เป็น PDF สแกน ต้อง OCR ก่อน (เช่นด้วย `ocrmypdf`) |

---

## ต่อยอดได้

- [ ] เก็บคำถามที่ตอบไม่ได้ลง log เพื่อรู้ว่ายังขาดเอกสารเรื่องอะไร
- [ ] ล็อกอินด้วยบัญชีมหาวิทยาลัย แล้วแยก index ตามภาควิชา
- [ ] streaming คำตอบ (SSE) ให้ตัวอักษรไหลทีละคำ
- [ ] reranker ขั้นที่สองเพื่อเพิ่มความแม่นก่อนส่งเข้า LLM
