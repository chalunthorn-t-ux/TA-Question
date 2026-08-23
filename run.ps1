# สั่งรันเซิร์ฟเวอร์สำหรับพัฒนา
#
#   .\run.ps1            เปิดเฉพาะเครื่องนี้ (ปลอดภัยที่สุด — ค่าเริ่มต้น)
#   .\run.ps1 -Share     เปิดให้เครื่องอื่นในวง LAN เดียวกันเข้าได้ (สำหรับให้คนอื่นดู)
#
param(
    [switch]$Share,
    [int]$Port = 8000
)

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

if ($Share) {
    # หา IP ในวง LAN เพื่อบอก URL ที่เครื่องอื่นใช้เข้า
    $ip = (Get-NetIPAddress -AddressFamily IPv4 |
           Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
           Select-Object -First 1).IPAddress

    Write-Host ""
    Write-Host "==> โหมดแชร์: เครื่องอื่นในวง LAN เดียวกันเข้าได้" -ForegroundColor Yellow
    Write-Host "    เครื่องนี้      : http://127.0.0.1:$Port" -ForegroundColor Green
    Write-Host "    เครื่องอื่นใช้  : http://${ip}:$Port" -ForegroundColor Green
    Write-Host ""
    Write-Host "    ครั้งแรก Windows Firewall จะถามว่าอนุญาตไหม -> กด Allow" -ForegroundColor DarkGray
    Write-Host "    ระบบนี้ยังไม่มีระบบล็อกอิน ใครอยู่ในวงเดียวกันและรู้ URL ก็เข้าได้" -ForegroundColor DarkGray
    Write-Host "    ปิดเซิร์ฟเวอร์ด้วย Ctrl+C เมื่อใช้เสร็จ" -ForegroundColor DarkGray
    Write-Host ""

    # --reload ไม่เหมาะกับโหมดแชร์ เพราะรีสตาร์ทกลางทางจะทำให้คนที่กำลังดูสะดุด
    & $python -m uvicorn app.main:app --host 0.0.0.0 --port $Port
}
else {
    Write-Host "==> เปิดเซิร์ฟเวอร์ที่ http://127.0.0.1:$Port" -ForegroundColor Green
    Write-Host "    (ถ้าต้องการให้เครื่องอื่นดูด้วย ใช้:  .\run.ps1 -Share)" -ForegroundColor DarkGray
    & $python -m uvicorn app.main:app --reload --host 127.0.0.1 --port $Port
}
