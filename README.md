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

## Deploy บน Vercel

Vercel รันแบบ serverless — **ดิสก์อ่านได้อย่างเดียว** ทุกอย่างที่ต้องเขียนจึงต้องออกไปอยู่ข้างนอก

| ของ | อยู่ที่ | อัปเดตยังไง |
|---|---|---|
| บัญชีผู้ใช้ + log คำถามที่ตอบไม่ได้ | Postgres (Neon) | เว็บเขียนเองได้ตามปกติ |
| index ความรู้ (`index.json` + `vectors.npy`) | Vercel Blob | build ในเครื่องแล้ว push |
| เอกสารที่อัปผ่านหน้าเว็บ | Vercel Blob ใต้ `docs/` | ดึงลงเครื่องด้วย `pull_docs.py` |

### 1. ผูกที่เก็บข้อมูล

Vercel → **Storage** → Marketplace → **Neon** → Create → Connect to project (ได้ `DATABASE_URL`)
Vercel → **Storage** → **Blob** → Create → Connect to project (ได้ `BLOB_READ_WRITE_TOKEN`)

ใช้ connection string แบบ **pooled** (มี `-pooler`) เสมอ เพราะ serverless เปิด connection ใหม่ทุก request
ก็อปทั้งสองค่ามาใส่ `.env` ในเครื่องด้วย เพราะสคริปต์ฝั่ง dev ต้องใช้

### 2. ตั้ง environment variable ที่ Vercel

| ตัวแปร | จำเป็น | หมายเหตุ |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | ไม่มีก็ตอบคำถามไม่ได้ |
| `SESSION_SECRET` | ✅ | **ตั้งเองเสมอ** ไม่งั้นคีย์เปลี่ยนทุก cold start = ล็อกอินไม่ผ่านสักครั้ง |
| `SESSION_HTTPS_ONLY` | ✅ | ตั้ง `1` |
| `DATABASE_URL` | ✅ | Neon ฉีดให้ตอน Connect |
| `BLOB_READ_WRITE_TOKEN` | ✅ | Blob ฉีดให้ตอน Connect |
| `INDEX_BLOB_PREFIX` | แนะนำ | ค่าสุ่มยาว ๆ เช่น `idx-9f3a1c7b8e` — ดูหัวข้อความปลอดภัยข้างล่าง |

สร้าง `SESSION_SECRET` ด้วย `python -c "import secrets;print(secrets.token_urlsafe(48))"`
⚠️ ตั้งแล้วห้ามเปลี่ยน ไม่งั้นทุกคนหลุดออกจากระบบพร้อมกัน

### 3. ย้ายบัญชีเดิมเข้าฐานข้อมูล (ทำครั้งเดียว)

```bash
python scripts/users.py init-db
python scripts/users.py migrate
```

`migrate` ย้าย hash รหัสผ่านไปตรง ๆ — ทุกคนล็อกอินด้วยรหัสเดิมได้ทันที ไม่ต้องตั้งใหม่

### 4. รอบการอัปเดตความรู้

```bash
python scripts/pull_docs.py      # ถ้ามีคนอัปเอกสารผ่านหน้าเว็บ
python scripts/ingest.py --push  # build index แล้วส่งขึ้น Blob
```

เว็บจะหยิบ index ชุดใหม่ตอน cold start ถัดไป **ไม่ต้อง deploy ใหม่**
ปุ่ม "สร้าง Index" บนเว็บจะตอบ 409 พร้อมบอกขั้นตอนนี้ เพราะ serverless มีเพดานเวลา 60 วินาที
ถ้าปล่อยให้เริ่มแล้วไปตายกลางทาง จะได้ index ครึ่ง ๆ กลาง ๆ ซึ่งแย่กว่าไม่ทำเลย

### 5. ตรวจว่าขึ้นครบ

เปิด `/healthz` — `problems` ต้องว่าง และดูที่ `checks`:

```jsonc
"user_storage": "postgres",   // ไม่ใช่ "json"
"index_source": "blob",       // ไม่ใช่ "disk" หรือ "none"
"blob_storage": true,
"session_secret": true
```

จากนั้นล็อกอินแล้วรีเฟรชหลาย ๆ ครั้งห่างกันสัก 1 นาที ต้องไม่เด้งกลับหน้า login

### ⚠️ ความปลอดภัยของ Blob

blob ที่อัปเป็นแบบ public — **ใครรู้ URL ก็เปิดอ่าน `index.json` ได้ทั้งไฟล์**
ซึ่งข้างในคือเนื้อหาเอกสารจริงทั้งหมด ให้ตั้ง `INDEX_BLOB_PREFIX` เป็นค่าสุ่มยาว ๆ
แล้วปฏิบัติกับมันเหมือนรหัสผ่าน ถ้ารับความเสี่ยงนี้ไม่ได้ ทางเลือกคือย้าย index
ไปเก็บเป็น `bytea` ใน Postgres แทน — แก้ที่ [`app/blobstore.py`](app/blobstore.py) ไฟล์เดียว

---

## โครงสร้างโปรเจกต์

```
C:\Knowleage\
├── app/
│   ├── config.py          อ่านค่าทั้งหมดจาก .env
│   ├── main.py            FastAPI: หน้าเว็บ + REST API
│   ├── auth.py            กฎเรื่องรหัสผ่านและสิทธิ์
│   ├── users_repo.py      บัญชีเก็บที่ไหน: Postgres หรือไฟล์ JSON
│   ├── db.py              connection + schema ของ Postgres
│   ├── blobstore.py       อ่าน/เขียน Vercel Blob
│   ├── gaps.py            เก็บคำถามที่ระบบตอบไม่ได้
│   └── rag/
│       ├── loaders.py     PDF/Word/Excel/CSV/MD → RawSection
│       ├── chunker.py     ตัด chunk แบบ recursive (รองรับภาษาไทย)
│       ├── embedder.py    Gemini embedding + fallback แบบ local
│       ├── store.py       vector store + ค้นแบบ hybrid (cosine + BM25)
│       ├── vision.py      อ่านตาราง/ภาพในเอกสารด้วย Gemini Vision (+ cache)
│       ├── llm.py         prompt + เรียก Gemini generateContent (retry อัตโนมัติ)
│       └── pipeline.py    ingest() ask() และดึง index จาก Blob
├── templates/index.html   หน้าเว็บ
├── static/css|js/         ธีมและ logic ฝั่ง client
├── data/                  📥 เอกสารจริง — gitignore ไว้ ไม่ขึ้น repo
├── source-pdf/            📄 PDF สำรอง — ไม่ index (ดูหัวข้อ Word vs PDF)
├── samples/               🧪 ข้อมูลสมมติสำหรับ demo — ขึ้น repo ได้
├── storage/               📦 index + บัญชี + vision cache — ไม่ขึ้น repo แล้ว
├── scripts/
│   ├── ingest.py          สร้าง index จาก CLI (--push = ส่งขึ้น Blob ต่อ)
│   ├── push_index.py      ส่ง index ที่มีอยู่ขึ้น Blob
│   ├── pull_docs.py       ดึงเอกสารที่อัปผ่านเว็บลงมาที่ data/
│   └── users.py           จัดการบัญชี + init-db / migrate
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
| `GET` | `/api/gaps` | คำถามที่ระบบตอบไม่ได้ (admin) |
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
| `DATABASE_URL` | (ว่าง) | Postgres สำหรับบัญชีผู้ใช้ — ว่าง = ใช้ `storage/users.json` |
| `BLOB_READ_WRITE_TOKEN` | (ว่าง) | Vercel Blob สำหรับ index — ว่าง = ใช้ `storage/` ในดิสก์ |
| `INDEX_BLOB_PREFIX` | (ว่าง) | โฟลเดอร์นำหน้าใน Blob ควรเป็นค่าสุ่ม |

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
| ล็อกอินแล้วเด้งกลับหน้า login ทุกครั้ง (บน Vercel) | ยังไม่ได้ตั้ง `SESSION_SECRET` เป็นค่าคงที่ |
| สมัครสมาชิกไม่ได้ บอกว่าเซิร์ฟเวอร์เขียนไฟล์ไม่ได้ | ยังไม่ได้ตั้ง `DATABASE_URL` |
| push index แล้วเว็บยังตอบด้วยของเก่า | instance เดิมยังอุ่นอยู่ รอ cold start หรือ redeploy — เช็ค `/healthz` → `index_source` |

---

## ต่อยอดได้

- [x] เก็บคำถามที่ตอบไม่ได้ลง log เพื่อรู้ว่ายังขาดเอกสารเรื่องอะไร — ตาราง `unanswered` + `GET /api/gaps`
- [ ] หน้า `/admin/gaps` แสดงผลแบบตาราง (ตอนนี้ยังมีแค่ JSON)
- [ ] ล็อกอินด้วยบัญชีมหาวิทยาลัย แล้วแยก index ตามภาควิชา
- [ ] streaming คำตอบ (SSE) ให้ตัวอักษรไหลทีละคำ
- [ ] reranker ขั้นที่สองเพื่อเพิ่มความแม่นก่อนส่งเข้า LLM
