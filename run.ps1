Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
$url = "http://127.0.0.1:8000"

# ถ้าเซิร์ฟเวอร์รันอยู่แล้ว เปิดเบราว์เซอร์อย่างเดียว ไม่ต้องรันซ้ำจนชนพอร์ต
try {
    Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 2 | Out-Null
    Write-Host "เซิร์ฟเวอร์รันอยู่แล้ว เปิด $url"
    Start-Process $url
    exit
} catch {}

if (-not (Test-Path .venv)) { py -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

# เปิดเบราว์เซอร์ให้เองเมื่อเซิร์ฟเวอร์พร้อม
Start-Job -ArgumentList $url {
    param($url)
    for ($i = 0; $i -lt 60; $i++) {
        try {
            Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 1 | Out-Null
            Start-Process $url
            break
        } catch { Start-Sleep 1 }
    }
} | Out-Null

Write-Host "กำลังเปิดเซิร์ฟเวอร์ที่ $url (กด Ctrl+C เพื่อหยุด)"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
