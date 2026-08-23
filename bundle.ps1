# แพ็กโปรเจกต์ทั้งชุดเป็นไฟล์ zip เดียว เพื่อเอาไปติดตั้งอีกเครื่อง
#
#   .\bundle.ps1              แพ็กพร้อมเอกสารและ index (ใช้ได้ทันทีที่ปลายทาง)
#   .\bundle.ps1 -NoSecrets   ไม่ใส่ .env และเอกสารจริง (ปลอดภัยกว่า แต่ปลายทางต้องเตรียมเอง)
#
# ⚠️ ไฟล์ zip ที่ได้มี API key และข้อมูลส่วนบุคคลของผู้เข้าอบรม
#    ให้ส่งผ่าน USB หรือที่เก็บส่วนตัวเท่านั้น ห้ามส่งในกลุ่มแชทหรือวางบนไดรฟ์ที่แชร์กันหลายคน
#
param(
    [switch]$NoSecrets,
    [string]$OutDir = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$name  = "ta-assistant-$stamp"
$stage = Join-Path $env:TEMP $name
$zip   = Join-Path $OutDir "$name.zip"

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null

Write-Host "==> กำลังแพ็ก..." -ForegroundColor Cyan

# --- โค้ดและไฟล์ตั้งค่า (จำเป็นเสมอ) ---
$always = @(
    "app", "static", "templates", "scripts", "samples", ".claude",
    "requirements.txt", "run.ps1", "bundle.ps1", "README.md",
    ".env.example", ".gitignore", ".gitattributes"
)
foreach ($item in $always) {
    $src = Join-Path $PSScriptRoot $item
    if (Test-Path $src) {
        Copy-Item $src -Destination $stage -Recurse -Force
    }
}

# --- ลบ __pycache__ ที่ติดมา ---
Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

# --- เอกสาร index และ API key ---
if ($NoSecrets) {
    Write-Host "    ข้าม .env / data / storage (โหมด -NoSecrets)" -ForegroundColor DarkGray
    Write-Host "    ปลายทางต้องใส่ API key และเอกสารเอง แล้วกด 'สร้าง Index'" -ForegroundColor DarkGray
}
else {
    foreach ($item in @("data", "storage", ".env")) {
        $src = Join-Path $PSScriptRoot $item
        if (Test-Path $src) {
            Copy-Item $src -Destination $stage -Recurse -Force
            Write-Host "    + $item" -ForegroundColor DarkGray
        }
    }
}

# --- คู่มือติดตั้งที่ปลายทาง ---
$readme = @"
วิธีติดตั้งที่เครื่องใหม่
==============================================================

สิ่งที่ต้องมีก่อน
  Python 3.11 ขึ้นไป  ->  https://www.python.org/downloads/
  ตอนติดตั้งอย่าลืมติ๊ก "Add Python to PATH"

ขั้นตอน
  1. แตกไฟล์ zip นี้ไปไว้ที่ไหนก็ได้ เช่น C:\ta-assistant
  2. เปิด PowerShell ที่โฟลเดอร์นั้น
  3. รัน:   .\run.ps1
     ครั้งแรกจะสร้าง virtual environment และติดตั้ง library เอง ใช้เวลาสัก 2-3 นาที
  4. เปิดเบราว์เซอร์ไปที่ http://127.0.0.1:8000

ถ้าอยากให้เครื่องอื่นในออฟฟิศเข้าดูด้วย
  รัน:   .\run.ps1 -Share
  แล้วเอา URL ที่มันพิมพ์ออกมาให้คนอื่นเปิด (ต้องอยู่วง LAN เดียวกัน)

ถ้าตอบคำถามไม่ได้ ให้ตรวจ 3 อย่างนี้
  1. ไฟล์ .env มี GEMINI_API_KEY อยู่ไหม
  2. โฟลเดอร์ storage/ มีไฟล์ vectors.npy กับ index.json ไหม
     ถ้าไม่มี -> กดปุ่ม "สร้าง Index" บนหน้าเว็บ
  3. เน็ตที่ออฟฟิศเข้า generativelanguage.googleapis.com ได้ไหม
     ทดสอบ:  curl.exe -I https://generativelanguage.googleapis.com
     ถ้าโดนบล็อก ต้องขอ IT เปิดให้

เพิ่มเอกสารใหม่
  วางไฟล์ .docx / .xlsx / .csv ในโฟลเดอร์ data\
  แล้วกดปุ่ม "สร้าง Index" บนหน้าเว็บ
  (แนะนำ Word มากกว่า PDF เพราะ PDF ทำสระอำและวรรณยุกต์ไทยหลุด)

หมายเหตุเรื่องข้อมูล
  โฟลเดอร์ data\ มีเอกสารที่มีชื่อผู้เข้าอบรมจริง
  ระบบยังไม่มีระบบล็อกอิน อย่าเปิด URL ทิ้งไว้หรือส่งต่อในกลุ่มแชท
"@
Set-Content -Path (Join-Path $stage "อ่านก่อนติดตั้ง.txt") -Value $readme -Encoding utf8

# --- บีบอัด ---
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "$stage\*" -DestinationPath $zip -CompressionLevel Optimal
Remove-Item $stage -Recurse -Force

$sizeMb = [math]::Round((Get-Item $zip).Length / 1MB, 2)

Write-Host ""
Write-Host "==> เสร็จแล้ว: $zip  ($sizeMb MB)" -ForegroundColor Green
if (-not $NoSecrets) {
    Write-Host ""
    Write-Host "    !! ไฟล์นี้มี API key และชื่อผู้เข้าอบรมจริงอยู่ข้างใน" -ForegroundColor Yellow
    Write-Host "       ส่งผ่าน USB หรือที่เก็บส่วนตัวเท่านั้น ห้ามส่งในกลุ่มแชท" -ForegroundColor Yellow
}
Write-Host ""
