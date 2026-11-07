#!/usr/bin/env bash
# รันเซิร์ฟเวอร์บน macOS/Linux ด้วยคำสั่งเดียว: ./run.sh
set -e
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
url="http://127.0.0.1:8000"

open_url() {
    if command -v open >/dev/null; then open "$1"; else xdg-open "$1" >/dev/null 2>&1 || true; fi
}

# ถ้าเซิร์ฟเวอร์รันอยู่แล้ว เปิดเบราว์เซอร์อย่างเดียว ไม่ต้องรันซ้ำจนชนพอร์ต
if curl -fs -o /dev/null --max-time 2 "$url"; then
    echo "เซิร์ฟเวอร์รันอยู่แล้ว เปิด $url"
    open_url "$url"
    exit 0
fi

if [ ! -f .env ]; then
    echo "ไม่พบไฟล์ .env (ต้องมี DATABASE_URL) ดูวิธีสร้างใน README.md" >&2
    exit 1
fi

# .venv เดิมสร้างด้วย uv ซึ่งไม่มี pip จึงใช้ uv ก่อนถ้ามี
if command -v uv >/dev/null; then
    [ -x .venv/bin/python ] || uv venv -q .venv
    uv pip install -q --python .venv/bin/python -r requirements.txt
else
    [ -x .venv/bin/python ] || python3 -m venv .venv
    .venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip -q
    .venv/bin/python -m pip install -q -r requirements.txt
fi

# เปิดเบราว์เซอร์ให้เองเมื่อเซิร์ฟเวอร์พร้อม
(
    for _ in $(seq 60); do
        if curl -fs -o /dev/null --max-time 1 "$url"; then open_url "$url"; break; fi
        sleep 1
    done
) &

echo "กำลังเปิดเซิร์ฟเวอร์ที่ $url (กด Ctrl+C เพื่อหยุด)"
exec .venv/bin/python -m uvicorn app.main:app --reload
