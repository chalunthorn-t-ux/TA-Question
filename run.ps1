# สั่งรันเซิร์ฟเวอร์สำหรับพัฒนา:  .\run.ps1
$ErrorActionPreference = "Stop"

$venv = Join-Path $PSScriptRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "==> สร้าง virtual environment..." -ForegroundColor Cyan
    py -3 -m venv $venv
    & $python -m pip install --upgrade pip --quiet
    Write-Host "==> ติดตั้ง dependencies..." -ForegroundColor Cyan
    & $python -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
}

if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    Copy-Item (Join-Path $PSScriptRoot ".env.example") (Join-Path $PSScriptRoot ".env")
    Write-Host "==> สร้างไฟล์ .env แล้ว — ใส่ GEMINI_API_KEY ก่อนใช้งานจริง" -ForegroundColor Yellow
}

Write-Host "==> เปิดเซิร์ฟเวอร์ที่ http://127.0.0.1:8000" -ForegroundColor Green
& $python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
