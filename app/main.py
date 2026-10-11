from __future__ import annotations

import io
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from math import asin, cos, pi, radians, sin, sqrt
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
# ตั้งชื่อใหม่เพราะ fastapi.Request ชื่อชนกัน ถ้าปล่อยไว้ตัวหลังจะทับตัวแรก
from urllib.request import Request as HttpRequest, urlopen

import psycopg
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scripts import import_csv, import_excel, refresh_data

from . import ai_summary, analytics, auth, business
from .database import close_pool, get_pool, initialize_database

BASE_DIR = Path(__file__).resolve().parent.parent
PATTAYA_LATITUDE = 12.9236
PATTAYA_LONGITUDE = 100.8694
USER_AGENT = "PattayaSmartTourism/0.1 (academic prototype)"
THAI_MONTH_NAMES = (
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
)

# วันนี้ตามเวลาไทย ฐานข้อมูล Supabase ใช้เวลา UTC ถ้าใช้ CURRENT_DATE ตรง ๆ ช่วงตี 0-7 จะได้วันของเมื่อวาน
TODAY_BANGKOK = "(now() AT TIME ZONE 'Asia/Bangkok')::date"

# ตาราง public_holidays เก็บทั้งวันหยุดราชการ (public/observed) และวันสำคัญที่ไม่ใช่วันหยุด
# (observance เช่น ตรุษจีน คริสต์มาส วาเลนไทน์) เวลานับ "จำนวนวันหยุดต่อเดือน" เพื่อวิเคราะห์
# ต้องนับเฉพาะวันที่ราชการหยุดจริง ไม่งั้นผลของวันหยุดจะถูกเจือจางด้วยวันที่คนยังทำงานปกติ
# COALESCE ไว้เผื่อแถวเก่าที่นำเข้าก่อนมีการแยกประเภท
PUBLIC_HOLIDAY_ONLY = "COALESCE(holiday_type, 'public') <> 'observance'"

HOLIDAY_SOURCE = "Thai Public Holidays Calendar (iCalendar)"

# ประเภทสถานที่ใน OpenStreetMap เป็นแท็กภาษาอังกฤษ แปลเฉพาะตอนแสดงผล ในฐานข้อมูลยังเก็บแท็กเดิมไว้ตรวจย้อนได้
POI_CATEGORY_LABELS = {
    "bar": "บาร์", "restaurant": "ร้านอาหาร", "guest_house": "เกสต์เฮาส์", "cafe": "คาเฟ่", "hotel": "โรงแรม",
    "apartment": "อพาร์ตเมนต์ให้เช่า", "nightclub": "ไนต์คลับ", "hostel": "โฮสเทล", "motel": "โมเทล",
    "theme_park": "สวนสนุก", "attraction": "สถานที่ท่องเที่ยว", "museum": "พิพิธภัณฑ์", "artwork": "งานศิลปะ",
    "viewpoint": "จุดชมวิว", "marketplace": "ตลาด", "information": "ศูนย์ข้อมูลท่องเที่ยว", "zoo": "สวนสัตว์",
    "spa_resort": "สปารีสอร์ต", "mall": "ห้างสรรพสินค้า", "camp_site": "ลานกางเต็นท์", "picnic_site": "จุดปิกนิก",
    "gallery": "แกลเลอรี", "aquarium": "พิพิธภัณฑ์สัตว์น้ำ", "chalet": "บ้านพักตากอากาศ", "resort": "รีสอร์ต",
    # OSM บางจุดใส่แค่ tourism=yes ไม่ได้บอกประเภท
    "yes": "ไม่ระบุประเภท", "other": "อื่น ๆ",
}
THAI_MONTH_SHORT = ("ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.")


def category_label(category: str | None) -> str:
    return POI_CATEGORY_LABELS.get(category or "other", category or "อื่น ๆ")


def thai_date(value: Any) -> str:
    """วันที่ (date หรือข้อความ YYYY-MM-DD) เป็นรูปแบบที่อ่านง่าย เช่น 13 ต.ค. 2026"""
    text = str(value)
    return f"{int(text[8:10])} {THAI_MONTH_SHORT[int(text[5:7]) - 1]} {text[:4]}"

# อัปเดตข้อมูลจาก API ภายนอกเองระหว่างที่เซิร์ฟเวอร์เปิดอยู่ ตั้ง AUTO_REFRESH=0 ใน .env เพื่อปิด
AUTO_REFRESH = os.environ.get("AUTO_REFRESH", "1") == "1"
REFRESH_FIRST_CHECK_SECONDS = 30
REFRESH_CHECK_SECONDS = 3600

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_EXTENSIONS = (".csv", ".xlsx")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    auth.bind(query, query_one)
    auth.seed_roles(query, query_one)
    generated_password = auth.seed_admin(query, query_one)
    if generated_password:
        # แสดงครั้งเดียวตอนสร้างผู้ดูแลคนแรก ไม่ได้เก็บไว้ที่ไหน ถ้าพลาดต้องลบผู้ใช้แล้วให้ระบบสร้างใหม่
        # ต้อง flush เอง เพราะ stdout ถูก buffer เมื่อไม่ได้ต่อกับ terminal ข้อความจะค้างในบัฟเฟอร์
        # จนกว่าเซิร์ฟเวอร์จะปิด ซึ่งสายเกินไปสำหรับรหัสผ่านที่ต้องจดทันที
        banner = (
            "=" * 72
            + f"\n  สร้างผู้ดูแลระบบคนแรกแล้ว  ชื่อผู้ใช้: {os.environ.get('ADMIN_USERNAME', 'admin')}"
            + f"\n  รหัสผ่าน: {generated_password}"
            + "\n  บันทึกไว้ทันที ข้อความนี้จะไม่แสดงอีก"
            + "\n  ตั้งเองได้ด้วย ADMIN_PASSWORD ใน .env หรือ scripts/manage_users.py reset\n"
            + "=" * 72
        )
        print(banner, flush=True)
    stop_refresh = threading.Event()
    if AUTO_REFRESH:
        threading.Thread(target=refresh_loop, args=(stop_refresh,), daemon=True).start()
    yield
    stop_refresh.set()
    close_pool()


app = FastAPI(title="Pattaya Smart Tourism API", version="0.2.0", lifespan=lifespan)
# ตั้งแต่ใช้คุกกี้ session จะเปิด allow_origins=["*"] คู่กับ allow_credentials ไม่ได้
# เบราว์เซอร์ปฏิเสธการใช้ wildcard กับคำขอที่แนบ credential อยู่แล้ว และเปิดกว้างแบบนั้นก็เสี่ยงถูกสวมสิทธิ์
# แดชบอร์ดเสิร์ฟจาก origin เดียวกับ API อยู่แล้ว รายการนี้จึงมีไว้เผื่อเรียกจากเครื่องอื่นตอนพัฒนาเท่านั้น
ALLOWED_ORIGINS = [
    origin.strip() for origin in
    os.environ.get("ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """บังคับให้เบราว์เซอร์ถามเซิร์ฟเวอร์ก่อนใช้ไฟล์หน้าเว็บจากแคชทุกครั้ง

    ถ้าไม่ส่ง Cache-Control เบราว์เซอร์จะเดาอายุแคชเอง แล้วใช้ app.js เวอร์ชันเก่าต่อโดยไม่ถามเซิร์ฟเวอร์
    ซึ่งเคยทำให้หน้าเว็บที่แคชไว้ก่อนมีระบบล็อกอิน ยิง API ตรง ๆ จนได้ 401 ทั้งหน้า
    no-cache ไม่ได้แปลว่าห้ามแคช แต่แปลว่าต้องตรวจสอบก่อนใช้ ถ้าไฟล์ไม่เปลี่ยนจะได้ 304 ซึ่งเบามาก
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def query(sql: str, parameters: list | tuple = ()) -> list[dict]:
    try:
        return _fetch_all(sql, parameters)
    except psycopg.OperationalError:
        # connection ที่ Supabase ตัดทิ้งไประหว่างว่าง pool จะคัดออกเอง ลองใหม่ครั้งเดียวด้วย connection ใหม่
        return _fetch_all(sql, parameters)


def _fetch_all(sql: str, parameters: list | tuple) -> list[dict]:
    with get_pool().connection() as connection:
        # ไม่มีพารามิเตอร์ให้ส่ง None เพื่อไม่ให้ psycopg ตีความ % ใน SQL เป็น placeholder
        cursor = connection.execute(sql, parameters or None)
        # INSERT/UPDATE/DELETE ที่ไม่มี RETURNING ไม่คืนแถว cursor.description จะเป็น None
        # ต้องเช็กก่อนเรียก fetchall() ไม่งั้น psycopg จะโยน ProgrammingError
        return cursor.fetchall() if cursor.description else []


def query_one(sql: str, parameters: list | tuple = ()) -> dict | None:
    rows = query(sql, parameters)
    return rows[0] if rows else None


# แคชผลเรียก API ภายนอก เพื่อไม่ให้ทุกครั้งที่เปิดแดชบอร์ดยิงไปที่ Open-Meteo ใหม่
_external_cache: dict[str, tuple[float, Any]] = {}


def cached_fetch(key: str, url: str, ttl_seconds: int) -> Any:
    now = time.time()
    cached = _external_cache.get(key)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1]
    request = HttpRequest(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    _external_cache[key] = (now, payload)
    return payload


# แคชผลวิเคราะห์ ข้อมูลนำเข้าเปลี่ยนไม่บ่อย แต่การเทียบหลายวิธีต้องคำนวณซ้ำหลายรอบ
_analysis_cache: dict[str, tuple[float, Any]] = {}
ANALYSIS_TTL_SECONDS = 900


def cached_analysis(key: str, build) -> Any:
    now = time.time()
    cached = _analysis_cache.get(key)
    if cached and now - cached[0] < ANALYSIS_TTL_SECONDS:
        return cached[1]
    value = build()
    _analysis_cache[key] = (now, value)
    return value


def monthly_tourism_series() -> tuple[list[str], list[float]]:
    """อนุกรมจำนวนนักท่องเที่ยวรายเดือน คืน (ป้ายเดือน YYYY-MM, จำนวนคน) เรียงตามเวลา"""
    rows = query(
        """SELECT calendar_year, month_id, number_of_tourists
           FROM tourism_monthly ORDER BY calendar_year, month_id"""
    )
    labels = [f"{row['calendar_year']:04d}-{row['month_id']:02d}" for row in rows]
    return labels, [float(row["number_of_tourists"]) for row in rows]


def shift_month(label: str, steps: int) -> str:
    """เลื่อนป้ายเดือน YYYY-MM ไปข้างหน้า steps เดือน"""
    index = int(label[:4]) * 12 + int(label[5:7]) - 1 + steps
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def months_behind_today(label: str) -> int:
    """ข้อมูลล่าสุดช้ากว่าเดือนปัจจุบัน (เวลาไทย) กี่เดือน"""
    today = datetime.now(timezone(timedelta(hours=7)))
    return (today.year * 12 + today.month) - (int(label[:4]) * 12 + int(label[5:7]))


# ---------------------------------------------------------------- หน้าเว็บ


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


# ---------------------------------------------------------------- ยืนยันตัวตน


def audit(action: str, actor: str | None, target: str | None = None, details: str | None = None) -> None:
    query(
        "INSERT INTO audit_logs (action, actor, target, details) VALUES (%s, %s, %s, %s)",
        [action, actor, target, details],
    )


@app.post("/api/auth/login")
def login(response: Response, payload: dict = Body(...)) -> dict:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    row = query_one("SELECT id, username, password_hash, status FROM users WHERE username = %s", [username])
    if not row or not auth.verify_password(password, row["password_hash"]):
        audit("login_failed", username or "(ไม่ระบุ)")
        # ไม่บอกว่าผิดที่ชื่อผู้ใช้หรือรหัสผ่าน เพื่อไม่ให้ไล่เดาว่ามีชื่อผู้ใช้นี้อยู่จริงหรือไม่
        raise HTTPException(status_code=401, detail="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
    # ตรวจสถานะหลังตรวจรหัสผ่านผ่านแล้วเท่านั้น ถ้าตรวจก่อนจะกลายเป็นบอกใบ้ว่ามีบัญชีนี้อยู่
    if row["status"] == auth.STATUS_PENDING:
        audit("login_pending", row["username"])
        raise HTTPException(status_code=403, detail="บัญชีนี้รอผู้ดูแลระบบอนุมัติอยู่ กรุณาติดต่อผู้ดูแลระบบ")
    if row["status"] != auth.STATUS_ACTIVE:
        audit("login_rejected", row["username"])
        raise HTTPException(status_code=403, detail="บัญชีนี้ถูกระงับการใช้งาน กรุณาติดต่อผู้ดูแลระบบ")
    token = auth.create_session(query, row["id"])
    auth.set_session_cookie(response, token)
    audit("login", row["username"])
    return {"ok": True}


@app.post("/api/auth/register")
def register(request: Request, payload: dict = Body(...)) -> dict:
    """สมัครสมาชิกเอง บัญชีที่ได้จะอยู่ในสถานะรออนุมัติ ยังเข้าสู่ระบบไม่ได้จนกว่าผู้ดูแลจะกดอนุมัติ"""
    client = request.client.host if request.client else "unknown"
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "")).strip()
    organization = str(payload.get("organization", "")).strip()

    if not username.isascii() or not username.replace("_", "").replace(".", "").isalnum() or len(username) < 3:
        raise HTTPException(status_code=400, detail="ชื่อผู้ใช้ต้องเป็นภาษาอังกฤษหรือตัวเลข อย่างน้อย 3 ตัวอักษร")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="รหัสผ่านต้องยาวอย่างน้อย 8 ตัวอักษร")
    if role not in auth.SELF_SIGNUP_ROLES:
        raise HTTPException(status_code=400, detail="เลือกได้เฉพาะผู้บริหารหรือผู้ประกอบการ")

    # นับโควตาหลังตรวจความถูกต้องของข้อมูลแล้ว คำขอที่กรอกผิดจึงไม่กินโควตา
    # ไม่งั้นคนกรอกผิดไม่กี่ครั้งจะถูกกันออกไปทั้งชั่วโมง ส่วนการกันสร้างบัญชีรัว ๆ ยังทำได้เหมือนเดิม
    if not auth.signup_allowed(client):
        raise HTTPException(status_code=429, detail="สมัครถี่เกินไป กรุณารอสักครู่แล้วลองใหม่")

    # ชื่อซ้ำตอบเหมือนสมัครสำเร็จ เพื่อไม่ให้คนนอกใช้หน้าสมัครไล่เช็กว่ามีชื่อผู้ใช้ไหนอยู่ในระบบบ้าง
    # ผู้ดูแลจะเห็นว่าไม่มีคำขอใหม่เข้ามา ส่วนคนสมัครจะติดต่อผู้ดูแลเองเมื่อรอแล้วไม่ได้รับอนุมัติ
    if query_one("SELECT id FROM users WHERE username = %s", [username]):
        audit("register_duplicate", username, details=f"จาก {client}")
        return {"ok": True, "status": auth.STATUS_PENDING}

    user = query_one(
        """INSERT INTO users (username, organization, password_hash, status, requested_role)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        [username, organization or None, auth.hash_password(password), auth.STATUS_PENDING, role],
    )
    audit("register", username, details=f"ขอบทบาท {role} · {organization or 'ไม่ระบุหน่วยงาน'} · จาก {client}")
    return {"ok": True, "status": auth.STATUS_PENDING, "id": user["id"]}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    user = auth.load_user(request)
    auth.delete_session(query, request.cookies.get(auth.SESSION_COOKIE))
    auth.clear_session_cookie(response)
    if user:
        audit("logout", user["username"])
    return {"ok": True}


@app.post("/api/auth/password")
def change_password(request: Request, payload: dict = Body(...)) -> dict:
    """ผู้ใช้เปลี่ยนรหัสผ่านของตัวเอง ต้องยืนยันด้วยรหัสปัจจุบันเสมอ

    เรียกได้สองทาง: จากในระบบ (ใช้ session บอกว่าเป็นใคร) และจากหน้าเข้าสู่ระบบ (ส่ง username มาด้วย ไม่ต้องล็อกอินก่อน)
    ทั้งสองทางพิสูจน์ตัวด้วยรหัสปัจจุบันเหมือนกัน คนที่ลืมรหัสปัจจุบันยังต้องให้ผู้ดูแลรีเซ็ตให้
    """
    client = request.client.host if request.client else "unknown"
    current = str(payload.get("current_password", ""))
    new = str(payload.get("new_password", ""))
    session_user = auth.load_user(request)
    username = session_user["username"] if session_user else str(payload.get("username", "")).strip()
    if auth.password_guesses_exhausted(client):
        raise HTTPException(status_code=429, detail="ใส่รหัสผ่านผิดหลายครั้งเกินไป กรุณารอหนึ่งชั่วโมงแล้วลองใหม่")
    row = query_one("SELECT id, username, password_hash, status FROM users WHERE username = %s", [username])
    if not row or not auth.verify_password(current, row["password_hash"]):
        auth.record_password_guess(client)
        audit("change_password_failed", username or "(ไม่ระบุ)", details=f"จาก {client}")
        # จากหน้าเข้าสู่ระบบ ไม่บอกว่าผิดที่ชื่อผู้ใช้หรือรหัสผ่าน เหตุผลเดียวกับตอนล็อกอิน
        raise HTTPException(status_code=400, detail=(
            "รหัสผ่านปัจจุบันไม่ถูกต้อง" if session_user else "ชื่อผู้ใช้หรือรหัสผ่านปัจจุบันไม่ถูกต้อง"
        ))
    # บัญชีที่รออนุมัติหรือถูกระงับเปลี่ยนรหัสไม่ได้ ตรวจหลังรหัสผ่านถูกแล้วเท่านั้น จะได้ไม่บอกใบ้ว่ามีบัญชีนี้อยู่
    if row["status"] != auth.STATUS_ACTIVE:
        raise HTTPException(status_code=403, detail="บัญชีนี้ยังใช้งานไม่ได้ กรุณาติดต่อผู้ดูแลระบบ")
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="รหัสผ่านใหม่ต้องยาวอย่างน้อย 8 ตัวอักษร")
    if new == current:
        raise HTTPException(status_code=400, detail="รหัสผ่านใหม่ต้องไม่ซ้ำกับรหัสเดิม")
    query("UPDATE users SET password_hash = %s WHERE id = %s", [auth.hash_password(new), row["id"]])
    # ตัด session ที่ค้างอยู่ทั้งหมด ยกเว้นเครื่องที่กดเปลี่ยนจากในระบบ ถ้ารหัสเดิมรั่ว คนที่ล็อกอินค้างไว้ที่อื่นจะหลุดทันที
    removed = query(
        "DELETE FROM user_sessions WHERE user_id = %s AND token_hash <> %s RETURNING token_hash",
        [row["id"], auth.hash_token(request.cookies.get(auth.SESSION_COOKIE, "")) if session_user else ""],
    )
    audit("change_password", row["username"], details=(
        f"{'จากในระบบ' if session_user else 'จากหน้าเข้าสู่ระบบ'} · ยกเลิก session ที่ค้างอยู่ {len(removed)} รายการ"
    ))
    return {"ok": True, "other_sessions_removed": len(removed)}


@app.get("/api/auth/me")
def me(request: Request) -> dict:
    """หน้าเว็บเรียกตอนเปิดหน้า เพื่อรู้ว่าต้องแสดงหน้าล็อกอินหรือซ่อนส่วนไหนบ้าง

    ไม่โยน 401 เพราะ "ยังไม่ได้เข้าสู่ระบบ" เป็นคำตอบปกติของ endpoint นี้ ไม่ใช่ข้อผิดพลาด
    """
    user = auth.load_user(request)
    return {"authenticated": bool(user), "user": user}


# ---------------------------------------------------------------- จัดการผู้ใช้ (เฉพาะผู้ดูแลระบบ)


@app.get("/api/admin/roles")
def list_roles(_: dict = Depends(auth.require("manage:users"))) -> list[dict]:
    return query(
        """SELECT r.name, r.description,
                  array_agg(rp.permission ORDER BY rp.permission) AS permissions,
                  (SELECT COUNT(*) FROM user_roles ur WHERE ur.role_id = r.id) AS users
           FROM roles r LEFT JOIN role_permissions rp ON rp.role_id = r.id
           GROUP BY r.id, r.name, r.description ORDER BY r.name"""
    )


@app.get("/api/admin/users")
def list_users(_: dict = Depends(auth.require("manage:users"))) -> list[dict]:
    return query(
        """SELECT u.id, u.username, u.full_name, u.email, u.status, u.requested_role, u.organization,
                  to_char(u.created_at AT TIME ZONE 'Asia/Bangkok', 'YYYY-MM-DD HH24:MI') AS created_at,
                  COALESCE(array_agg(r.name) FILTER (WHERE r.name IS NOT NULL), '{}') AS roles
           FROM users u
           LEFT JOIN user_roles ur ON ur.user_id = u.id
           LEFT JOIN roles r ON r.id = ur.role_id
           GROUP BY u.id
           -- คำขอที่รออนุมัติขึ้นก่อนเสมอ ผู้ดูแลจะได้เห็นงานค้างทันทีที่เปิดหน้า
           ORDER BY (u.status = 'pending') DESC, u.created_at DESC, u.username"""
    )


@app.post("/api/admin/users")
def create_user(payload: dict = Body(...), actor: dict = Depends(auth.require("manage:users"))) -> dict:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    role_name = str(payload.get("role", "")).strip()
    if not username or len(password) < 8 or role_name not in auth.ROLE_DEFINITIONS:
        raise HTTPException(status_code=400, detail="ต้องระบุชื่อผู้ใช้ รหัสผ่านอย่างน้อย 8 ตัวอักษร และบทบาทที่มีอยู่จริง")
    if query_one("SELECT id FROM users WHERE username = %s", [username]):
        raise HTTPException(status_code=409, detail="มีชื่อผู้ใช้นี้อยู่แล้ว")
    user = query_one(
        "INSERT INTO users (username, full_name, email, password_hash) VALUES (%s, %s, %s, %s) RETURNING id",
        [username, payload.get("full_name") or None, payload.get("email") or None, auth.hash_password(password)],
    )
    role = query_one("SELECT id FROM roles WHERE name = %s", [role_name])
    query("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)", [user["id"], role["id"]])
    audit("create_user", actor["username"], username, f"บทบาท {role_name}")
    return {"ok": True, "id": user["id"]}


@app.post("/api/admin/users/{user_id}/approve")
def approve_user(user_id: int, payload: dict = Body(default={}), actor: dict = Depends(auth.require("manage:users"))) -> dict:
    """อนุมัติคำขอสมัคร แล้วให้บทบาทตามที่ผู้ดูแลเลือก (ค่าเริ่มต้นคือบทบาทที่ผู้สมัครขอมา)"""
    target = query_one("SELECT username, status, requested_role FROM users WHERE id = %s", [user_id])
    if not target:
        raise HTTPException(status_code=404, detail="ไม่พบผู้ใช้นี้")
    if target["status"] == auth.STATUS_ACTIVE:
        raise HTTPException(status_code=409, detail="บัญชีนี้ใช้งานได้อยู่แล้ว")
    role_name = str(payload.get("role") or target["requested_role"] or "").strip()
    if role_name not in auth.ROLE_DEFINITIONS:
        raise HTTPException(status_code=400, detail="ต้องระบุบทบาทที่มีอยู่จริง")
    role = query_one("SELECT id FROM roles WHERE name = %s", [role_name])
    # ให้บทบาทเดียวเสมอ ลบของเดิมก่อนกันกรณีอนุมัติซ้ำหลังเคยถูกปฏิเสธ
    query("DELETE FROM user_roles WHERE user_id = %s", [user_id])
    query("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)", [user_id, role["id"]])
    query(
        "UPDATE users SET status = %s, reviewed_at = now(), reviewed_by = %s WHERE id = %s",
        [auth.STATUS_ACTIVE, actor["username"], user_id],
    )
    audit("approve_user", actor["username"], target["username"], f"ให้บทบาท {role_name}")
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reject")
def reject_user(user_id: int, actor: dict = Depends(auth.require("manage:users"))) -> dict:
    """ปฏิเสธหรือระงับบัญชี เก็บแถวไว้เป็นหลักฐานแทนการลบทิ้ง"""
    target = query_one("SELECT username FROM users WHERE id = %s", [user_id])
    if not target:
        raise HTTPException(status_code=404, detail="ไม่พบผู้ใช้นี้")
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="ระงับบัญชีของตัวเองไม่ได้")
    query(
        "UPDATE users SET status = %s, reviewed_at = now(), reviewed_by = %s WHERE id = %s",
        [auth.STATUS_REJECTED, actor["username"], user_id],
    )
    # ตัด session ที่ค้างอยู่ ไม่งั้นคนที่เพิ่งถูกระงับยังใช้คุกกี้เดิมต่อได้จนหมดอายุ
    query("DELETE FROM user_sessions WHERE user_id = %s", [user_id])
    audit("reject_user", actor["username"], target["username"])
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, actor: dict = Depends(auth.require("manage:users"))) -> dict:
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="ลบบัญชีของตัวเองไม่ได้")
    target = query_one("SELECT username FROM users WHERE id = %s", [user_id])
    if not target:
        raise HTTPException(status_code=404, detail="ไม่พบผู้ใช้นี้")
    remaining = query_one(
        """SELECT COUNT(*) AS total FROM user_roles ur
           JOIN roles r ON r.id = ur.role_id
           JOIN users u ON u.id = ur.user_id
           WHERE r.name = 'admin' AND u.status = 'active' AND ur.user_id <> %s""",
        [user_id],
    )
    if not remaining["total"]:
        raise HTTPException(status_code=400, detail="ต้องเหลือผู้ดูแลระบบอย่างน้อยหนึ่งคน")
    # ลบ session ของผู้ใช้ด้วย ไม่งั้นคนที่ถูกลบยังใช้คุกกี้เดิมต่อได้จนกว่าจะหมดอายุ
    query("DELETE FROM user_sessions WHERE user_id = %s", [user_id])
    query("DELETE FROM user_roles WHERE user_id = %s", [user_id])
    query("DELETE FROM users WHERE id = %s", [user_id])
    audit("delete_user", actor["username"], target["username"])
    return {"ok": True}


@app.get("/api/admin/audit")
def audit_log(limit: int = Query(default=50, ge=1, le=500), _: dict = Depends(auth.require("manage:users"))) -> list[dict]:
    return query(
        """SELECT action, actor, target, details,
                  to_char(timestamp AT TIME ZONE 'Asia/Bangkok', 'YYYY-MM-DD HH24:MI:SS') AS timestamp
           FROM audit_logs ORDER BY timestamp DESC, id DESC LIMIT %s""",
        [limit],
    )


# ---------------------------------------------------------------- จัดการข้อมูล (นำเข้าไฟล์ / อัปเดตอัตโนมัติ)


def store_upload(filename: str, data: bytes) -> dict:
    with get_pool().connection() as connection:
        if filename.lower().endswith(".csv"):
            return import_csv.import_rows(connection, filename, import_csv.decode_csv(data, filename))

        import_id = import_csv.create_import_record(connection, None, "excel", filename, filename, None, 0)
        result = {"import_id": import_id, "filename": filename, "label": "รายงาน Excel", "records_total": 0,
                  "records_success": 0, "records_failed": 0, "notes": [], "errors": []}
        try:
            # pool เป็น autocommit ต้องเปิด transaction เอง ถ้าไฟล์พังกลางทาง ข้อมูลเดิมของไฟล์นี้จะยังอยู่ครบ
            with connection.transaction():
                counts = import_excel.import_workbook(io.BytesIO(data), connection, filename)
        except Exception as exc:
            result["errors"] = [{"row_number": None, "error_text": f"อ่านไฟล์ Excel ไม่ได้: {exc}"}]
            connection.execute(
                "INSERT INTO import_errors (import_id, error_text) VALUES (%s, %s)", (import_id, result["errors"][0]["error_text"])
            )
            return result
        result.update(records_total=counts["cells"], records_success=counts["cells"])
        result["notes"] = [f"{counts['sheets']} ชีต · ตัวชี้วัดของชีตพัทยา {counts['metrics']} รายการ"]
        connection.execute(
            "UPDATE imports SET records_total = %s, records_success = %s, notes = %s WHERE id = %s",
            (counts["cells"], counts["cells"], result["notes"][0], import_id),
        )
        import_csv.attach_data_source(connection, import_id, Path(filename).stem, "excel")
        return result


@app.post("/api/admin/imports")
async def upload_import(request: Request, filename: str = Query(...),
    actor: dict = Depends(auth.require("manage:data")),
) -> dict:
    """รับไฟล์เป็น body ดิบ ส่วนชื่อไฟล์มาทาง query ไม่ใช้ multipart เพราะต้องเพิ่มไลบรารี python-multipart"""
    # เบราว์เซอร์บน Windows บางตัวส่งมาทั้ง path เก็บเฉพาะชื่อไฟล์
    name = Path(filename.replace("\\", "/")).name.strip()
    if not name.lower().endswith(UPLOAD_EXTENSIONS):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ .csv และ .xlsx")
    too_large = HTTPException(status_code=413, detail=f"ไฟล์ใหญ่เกิน {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    # ตรวจจาก header ก่อน จะได้ไม่ต้องอ่านไฟล์ใหญ่เข้าหน่วยความจำทั้งก้อนแล้วค่อยปฏิเสธ
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
        raise too_large
    data = await request.body()
    if len(data) > MAX_UPLOAD_BYTES:
        raise too_large
    if not data:
        raise HTTPException(status_code=400, detail="ไฟล์ว่าง")
    try:
        result = await run_in_threadpool(store_upload, name, data)
    except ValueError as exc:  # ถอดรหัสตัวอักษรของไฟล์ไม่ได้
        raise HTTPException(status_code=400, detail=str(exc))
    # ผลพยากรณ์และผลกระทบคำนวณจากข้อมูลที่เพิ่งเปลี่ยน ต้องคำนวณใหม่
    _analysis_cache.clear()
    audit(
        "import_failed" if result["errors"] else "import_data", actor["username"], name,
        f"{result['label'] or 'ไม่รู้จักรูปแบบ'} · สำเร็จ {result['records_success']} จาก {result['records_total']} แถว",
    )
    return result


@app.get("/api/admin/imports")
def list_imports(limit: int = Query(default=15, ge=1, le=200),
    _: dict = Depends(auth.require("manage:data")),
) -> list[dict]:
    return query(
        """SELECT i.id, COALESCE(i.raw_filename, i.source_name) AS name, i.import_type,
                  to_char(i.imported_at AT TIME ZONE 'Asia/Bangkok', 'YYYY-MM-DD HH24:MI') AS imported_at,
                  i.records_total, i.records_success, i.records_failed, i.notes,
                  (SELECT e.error_text FROM import_errors e WHERE e.import_id = i.id ORDER BY e.id LIMIT 1) AS first_error
           FROM imports i ORDER BY i.imported_at DESC, i.id DESC LIMIT %s""",
        [limit],
    )


def record_refresh(outcome: dict, actor: str) -> None:
    if outcome["ok"]:
        _analysis_cache.clear()
        audit("refresh_data", actor, outcome["label"], f"{outcome['records']} ระเบียน")
    elif outcome["first_failure"]:
        audit("refresh_failed", actor, outcome["label"], outcome["error"])


def refresh_loop(stop: threading.Event) -> None:
    """รันในเธรดเบื้องหลัง ตรวจทุกชั่วโมงว่ามีชุดข้อมูลไหนถึงรอบอัปเดต งานที่พังจะถูกลองใหม่ในรอบถัดไป"""
    delay = REFRESH_FIRST_CHECK_SECONDS
    while not stop.wait(delay):
        delay = REFRESH_CHECK_SECONDS
        try:
            for outcome in refresh_data.run():
                record_refresh(outcome, "system")
        except Exception as exc:  # ฐานข้อมูลต่อไม่ได้ชั่วคราว ไม่ควรทำให้เธรดตายแล้วไม่อัปเดตอีกเลย
            print(f"อัปเดตข้อมูลอัตโนมัติไม่สำเร็จ: {exc}", flush=True)


@app.get("/api/admin/refresh")
def refresh_status(_: dict = Depends(auth.require("manage:data"))) -> dict:
    return {
        "enabled": AUTO_REFRESH,
        "jobs": refresh_data.status(query(refresh_data.LAST_RUN_SQL, [refresh_data.SOURCE_NAMES])),
    }


@app.post("/api/admin/refresh/{key}")
def refresh_now(key: str, actor: dict = Depends(auth.require("manage:data"))) -> dict:
    if key not in refresh_data.JOBS:
        raise HTTPException(status_code=404, detail="ไม่พบชุดข้อมูลนี้")
    outcome = next(refresh_data.run([key], force=True))
    record_refresh(outcome, actor["username"])
    if not outcome["ok"]:
        raise HTTPException(status_code=502, detail=f"อัปเดตไม่สำเร็จ: {outcome['error']}")
    return outcome


# ---------------------------------------------------------------- สภาพอากาศ (Open-Meteo)


@app.get("/api/weather/live")
def live_weather(_: dict = Depends(auth.require("view:overview"))) -> dict:
    parameters = urlencode({
        "latitude": PATTAYA_LATITUDE,
        "longitude": PATTAYA_LONGITUDE,
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
        "timezone": "Asia/Bangkok",
    })
    payload = cached_fetch("weather_live", f"https://api.open-meteo.com/v1/forecast?{parameters}", 600)
    current = payload["current"]
    return {
        "location": "พัทยา",
        "observed_at": current["time"],
        "temperature_c": current["temperature_2m"],
        "humidity_percent": current["relative_humidity_2m"],
        "precipitation_mm": current["precipitation"],
        "wind_speed_kmh": current["wind_speed_10m"],
        "weather_code": current["weather_code"],
        "source": "Open-Meteo",
    }


@app.get("/api/weather/forecast")
def weather_forecast(days: int = Query(default=7, ge=1, le=16),
    _: dict = Depends(auth.require("view:overview")),
) -> list[dict]:
    """พยากรณ์รายวัน ใช้จับคู่กับวันหยุดเพื่อประเมินว่าช่วงวันหยุดที่จะถึงอากาศเอื้อหรือไม่"""
    parameters = urlencode({
        "latitude": PATTAYA_LATITUDE,
        "longitude": PATTAYA_LONGITUDE,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
        "timezone": "Asia/Bangkok",
        "forecast_days": days,
    })
    payload = cached_fetch(f"weather_forecast_{days}", f"https://api.open-meteo.com/v1/forecast?{parameters}", 1800)
    daily = payload["daily"]
    # Open-Meteo ส่งวันที่มาเป็นข้อความ จึงแปลงคอลัมน์ DATE เป็นข้อความก่อนจับคู่
    holiday_dates = {row["date"]: row["local_name"] or row["name"] for row in query(
        "SELECT date::text AS date, name, local_name FROM public_holidays"
    )}
    return [
        {
            "date": date,
            "weather_code": daily["weather_code"][index],
            "temperature_max_c": daily["temperature_2m_max"][index],
            "temperature_min_c": daily["temperature_2m_min"][index],
            "precipitation_mm": daily["precipitation_sum"][index],
            "precipitation_probability_percent": daily["precipitation_probability_max"][index],
            "holiday": holiday_dates.get(date),
        }
        for index, date in enumerate(daily["time"])
    ]


# ---------------------------------------------------------------- ภาพรวม


@app.get("/api/summary")
def summary(_: dict = Depends(auth.require("view:overview"))) -> dict:
    """KPI จากข้อมูลจริงทั้งหมด ไม่มีค่าจำลอง"""
    latest_tourism = query_one(
        """SELECT calendar_year, month_id, month_name, number_of_tourists, location
           FROM tourism_monthly ORDER BY calendar_year DESC, month_id DESC LIMIT 1"""
    )
    tourism_year = query_one(
        """SELECT calendar_year, SUM(number_of_tourists) AS tourists
           FROM tourism_monthly GROUP BY calendar_year ORDER BY calendar_year DESC LIMIT 1"""
    )
    tourism_previous = query_one(
        """SELECT SUM(number_of_tourists) AS tourists FROM tourism_monthly
           WHERE calendar_year = (SELECT MAX(calendar_year) - 1 FROM tourism_monthly)"""
    )
    traffic_year = query_one(
        """SELECT calendar_year, SUM(vehicle_volume) AS vehicles,
                  COUNT(DISTINCT intersection_name) AS intersections
           FROM zone_traffic_volume GROUP BY calendar_year ORDER BY calendar_year DESC LIMIT 1"""
    )
    poi_total = query_one("SELECT COUNT(*) AS total, COUNT(DISTINCT category) AS categories FROM poi_businesses")
    population = query_one(
        """SELECT calendar_year, SUM(population) AS population FROM demographic_profiles
           WHERE category = 'เพศ' GROUP BY calendar_year ORDER BY calendar_year DESC LIMIT 1"""
    )
    next_holiday = query_one(
        f"""SELECT date, name, local_name FROM public_holidays
            WHERE date >= {TODAY_BANGKOK} AND {PUBLIC_HOLIDAY_ONLY} ORDER BY date LIMIT 1"""
    )
    coverage = query_one(
        """SELECT COUNT(DISTINCT intersection_name) AS total,
                  COUNT(DISTINCT CASE WHEN latitude IS NOT NULL THEN intersection_name END) AS geocoded
           FROM zone_traffic_volume"""
    )

    tourists_this_year = (tourism_year or {}).get("tourists")
    tourists_last_year = (tourism_previous or {}).get("tourists")
    year_over_year = None
    if tourists_this_year and tourists_last_year:
        year_over_year = round((tourists_this_year - tourists_last_year) / tourists_last_year * 100, 1)

    return {
        "tourism": {
            "latest_month": latest_tourism,
            "year": (tourism_year or {}).get("calendar_year"),
            "tourists_year_total": tourists_this_year,
            "year_over_year_percent": year_over_year,
        },
        "traffic": traffic_year,
        "poi": poi_total,
        "population": population,
        "next_holiday": next_holiday,
        "geocode_coverage": coverage,
    }


# แหล่งข้อมูลถูกบันทึกด้วยชื่อไฟล์หรือชื่อ API ซึ่งคนนอกอ่านไม่รู้เรื่อง (เช่น "172-") ป้ายด้านล่างใช้ตอนแสดงผลเท่านั้น
DATASET_TABLE_LABELS = {
    "tourism_monthly": "นักท่องเที่ยวลงเกาะล้านรายเดือน",
    "demographic_profiles": "ประชากรแฝงของเมืองพัทยา",
    "zone_traffic_volume": "ปริมาณรถรายแยก",
    "boat_passengers_monthly": "ผู้โดยสารเรือไปเกาะล้าน",
    "air_quality_monthly": "ฝุ่น PM2.5 รายเดือน",
    "island_waste_monthly": "ขยะเกาะล้าน",
    "city_revenue_yearly": "รายได้ของเมืองรายปีงบประมาณ",
    "raw_excel_cells": "รายงาน Excel ของจังหวัดชลบุรี",
}
API_SOURCE_LABELS = {
    "Open-Meteo Archive (ERA5)": "สภาพอากาศรายวันย้อนหลัง",
    HOLIDAY_SOURCE: "วันหยุดและเทศกาลของไทย",
    "World Holidays API (TH)": "วันหยุดของไทย (แหล่งสำรอง)",
    "OpenStreetMap Overpass API": "สถานที่ท่องเที่ยวและธุรกิจ",
}


@app.get("/api/sources")
def sources(_: dict = Depends(auth.require("view:sources"))) -> list[dict]:
    """แหล่งที่มาของข้อมูลทุกชุดที่นำเข้า ใช้แสดงการอ้างอิงบนแดชบอร์ด"""
    # ไฟล์ไหนเป็นชุดข้อมูลอะไร ดูจากว่าตอนนี้แถวของไฟล์นั้นอยู่ในตารางไหน ไฟล์ที่ถูกไฟล์อื่นแทนที่ไปหมดแล้วจะไม่มีป้าย
    file_labels = {
        Path(row["source_file"]).stem: row["label"]
        for row in query(" UNION ALL ".join(
            f"SELECT DISTINCT source_file, '{label}' AS label FROM {table}" for table, label in DATASET_TABLE_LABELS.items()
        ))
    }
    # นำเข้าซ้ำได้หลายครั้ง จึงรายงานยอดของครั้งล่าสุดเท่านั้น ไม่ใช่ผลรวมสะสมของทุกครั้ง
    rows = query(
        """SELECT s.name, s.source_type, s.endpoint,
                  to_char(latest.imported_at AT TIME ZONE 'Asia/Bangkok', 'YYYY-MM-DD HH24:MI:SS')
                      AS last_imported_at,
                  latest.records_success AS records,
                  latest.records_failed AS records_failed
           FROM data_sources s
           LEFT JOIN imports latest ON latest.id = (
               SELECT id FROM imports WHERE data_source_id = s.id
               ORDER BY imported_at DESC, id DESC LIMIT 1
           )
           ORDER BY s.name"""
    )
    for row in rows:
        row["label"] = API_SOURCE_LABELS.get(row["name"]) or file_labels.get(row["name"])
    # ชุดที่รู้ว่าเป็นอะไรขึ้นก่อน เรียงตามชื่อไทย
    return sorted(rows, key=lambda row: (row["label"] is None, row["label"] or row["name"]))


# ---------------------------------------------------------------- โซน / แยก (แผนที่)

# รัศมีโดยประมาณรอบแยกที่นับว่าเป็น "ย่าน" เดียวกัน ใช้หาจำนวนสถานที่ท่องเที่ยว/ธุรกิจรอบแยก
# 0.005 องศา ~ 550 เมตร ที่ละติจูดของพัทยา
ZONE_RADIUS_DEGREES = 0.005


@app.get("/api/zones")
def zones(year: int | None = Query(default=None),
    _: dict = Depends(auth.require("view:spatial")),
) -> list[dict]:
    """แยกจริงจากชุดข้อมูลปริมาณรถ พร้อมจำนวนธุรกิจท่องเที่ยวรอบแยกจาก OpenStreetMap

    แทนที่ข้อมูลรายโซนจำลองเดิม ทุกค่าที่คืนมาอ้างอิงข้อมูลจริงทั้งหมด
    """
    clause = "WHERE calendar_year = %s" if year else ""
    parameters: list[Any] = [year] if year else []
    intersections = query(
        f"""SELECT intersection_name, latitude, longitude, geocode_method, geocode_detail,
                   SUM(vehicle_volume) AS vehicle_volume,
                   COUNT(DISTINCT calendar_year || '-' || month_id) AS months,
                   MIN(calendar_year) AS first_year, MAX(calendar_year) AS last_year
            FROM zone_traffic_volume {clause}
            GROUP BY intersection_name, latitude, longitude, geocode_method, geocode_detail
            ORDER BY vehicle_volume DESC""",
        parameters,
    )
    points = query("SELECT category, latitude, longitude FROM poi_businesses")
    for intersection in intersections:
        latitude, longitude = intersection["latitude"], intersection["longitude"]
        if latitude is None or longitude is None:
            intersection["poi_nearby"] = None
            intersection["poi_nearby_by_category"] = None
            continue
        nearby: dict[str, int] = {}
        for point in points:
            if (
                abs(point["latitude"] - latitude) <= ZONE_RADIUS_DEGREES
                and abs(point["longitude"] - longitude) <= ZONE_RADIUS_DEGREES
            ):
                nearby[point["category"]] = nearby.get(point["category"], 0) + 1
        intersection["poi_nearby"] = sum(nearby.values())
        intersection["poi_nearby_by_category"] = {
            category_label(category): count
            for category, count in sorted(nearby.items(), key=lambda item: item[1], reverse=True)[:5]
        }
        intersection["vehicle_volume_per_month"] = round(
            intersection["vehicle_volume"] / intersection["months"]
        ) if intersection["months"] else None
    return intersections


# ---------------------------------------------------------------- โซนย่อย (Micro-Zoning)

# ย่านท่องเที่ยวหลักของเมืองพัทยา เก็บเป็นจุดศูนย์กลางกับรัศมีเพื่อแบ่งพื้นที่ระดับย่อย
# พิกัดอ่านจาก OpenStreetMap เป็นค่าประมาณระดับย่าน ไม่ใช่ขอบเขตการปกครองตามกฎหมาย
# จุดที่อยู่ในรัศมีของหลายย่านจะถูกนับให้ย่านที่ศูนย์กลางใกล้ที่สุดเพียงย่านเดียว จึงไม่มีการนับซ้ำ
MICRO_ZONES: tuple[dict, ...] = (
    {"code": "naklua", "name": "นาเกลือ", "latitude": 12.9720, "longitude": 100.8940, "radius_km": 1.6},
    {"code": "north", "name": "พัทยาเหนือ", "latitude": 12.9560, "longitude": 100.8860, "radius_km": 1.4},
    {"code": "central", "name": "พัทยากลาง", "latitude": 12.9330, "longitude": 100.8810, "radius_km": 1.4},
    {"code": "walking", "name": "พัทยาใต้ / วอล์กกิ้งสตรีท", "latitude": 12.9245, "longitude": 100.8705, "radius_km": 1.2},
    {"code": "pratumnak", "name": "เขาพระตำหนัก", "latitude": 12.9110, "longitude": 100.8660, "radius_km": 1.3},
    {"code": "jomtien", "name": "หาดจอมเทียน", "latitude": 12.8930, "longitude": 100.8720, "radius_km": 2.2},
    {"code": "east", "name": "พัทยาตะวันออก (สุขุมวิท)", "latitude": 12.9330, "longitude": 100.9150, "radius_km": 3.0},
    {"code": "kolarn", "name": "เกาะล้าน", "latitude": 12.9180, "longitude": 100.7830, "radius_km": 2.5},
)

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """ระยะทางบนผิวโลกเป็นกิโลเมตร ใช้สูตร haversine"""
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi, d_lambda = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def assign_zone(latitude: float | None, longitude: float | None) -> Optional[dict]:
    """หาย่านที่ศูนย์กลางใกล้จุดนี้ที่สุดและยังอยู่ในรัศมี คืน None ถ้าอยู่นอกทุกย่าน"""
    if latitude is None or longitude is None:
        return None
    best: Optional[tuple[float, dict]] = None
    for zone in MICRO_ZONES:
        distance = haversine_km(latitude, longitude, zone["latitude"], zone["longitude"])
        if distance <= zone["radius_km"] and (best is None or distance < best[0]):
            best = (distance, zone)
    return best[1] if best else None


@app.get("/api/zones/micro")
def micro_zones(year: int | None = Query(default=None),
    _: dict = Depends(auth.require("view:spatial")),
) -> dict:
    """สรุปความหนาแน่นรายย่านของเมืองพัทยา ใช้ทำการวิเคราะห์การกระจายตัวระดับพื้นที่ย่อย

    รวมสถานที่ท่องเที่ยว/ธุรกิจจาก OpenStreetMap กับปริมาณรถรายแยกของเมืองพัทยา
    เข้าเป็นย่านเดียวกัน เพื่อเทียบได้ว่าคนกระจุกตัวอยู่ย่านไหนมากกว่ากัน
    """
    buckets: dict[str, dict] = {
        zone["code"]: {
            **zone,
            "area_km2": round(pi * zone["radius_km"] ** 2, 2),
            "poi_total": 0,
            "poi_by_category": {},
            "intersections": [],
            "vehicle_volume": 0,
            "vehicle_months": 0,
        }
        for zone in MICRO_ZONES
    }
    outside = {"poi_total": 0, "intersections": 0, "vehicle_volume": 0}

    for point in query("SELECT category, latitude, longitude FROM poi_businesses"):
        zone = assign_zone(point["latitude"], point["longitude"])
        if zone is None:
            outside["poi_total"] += 1
            continue
        bucket = buckets[zone["code"]]
        bucket["poi_total"] += 1
        categories = bucket["poi_by_category"]
        categories[point["category"]] = categories.get(point["category"], 0) + 1

    clause = "WHERE calendar_year = %s" if year else ""
    parameters: list[Any] = [year] if year else []
    intersections = query(
        f"""SELECT intersection_name, latitude, longitude,
                   SUM(vehicle_volume) AS vehicle_volume,
                   COUNT(DISTINCT calendar_year || '-' || month_id) AS months
            FROM zone_traffic_volume {clause}
            GROUP BY intersection_name, latitude, longitude
            ORDER BY vehicle_volume DESC""",
        parameters,
    )
    for intersection in intersections:
        zone = assign_zone(intersection["latitude"], intersection["longitude"])
        if zone is None:
            # แยกที่ยังไม่มีพิกัด หรืออยู่นอกย่านที่กำหนด ไม่ถูกนับให้ย่านใด
            outside["intersections"] += 1
            outside["vehicle_volume"] += int(intersection["vehicle_volume"])
            continue
        bucket = buckets[zone["code"]]
        bucket["intersections"].append(intersection["intersection_name"])
        bucket["vehicle_volume"] += int(intersection["vehicle_volume"])
        bucket["vehicle_months"] = max(bucket["vehicle_months"], intersection["months"])

    poi_grand_total = sum(bucket["poi_total"] for bucket in buckets.values()) + outside["poi_total"]
    results = []
    for bucket in buckets.values():
        categories = sorted(bucket["poi_by_category"].items(), key=lambda item: item[1], reverse=True)
        results.append({
            "code": bucket["code"],
            "name": bucket["name"],
            "latitude": bucket["latitude"],
            "longitude": bucket["longitude"],
            "radius_km": bucket["radius_km"],
            "area_km2": bucket["area_km2"],
            "poi_total": bucket["poi_total"],
            "poi_density_per_km2": round(bucket["poi_total"] / bucket["area_km2"], 1),
            "poi_share_percent": round(bucket["poi_total"] / poi_grand_total * 100, 1) if poi_grand_total else 0.0,
            "top_categories": {category_label(category): count for category, count in categories[:5]},
            "dominant_category": category_label(categories[0][0]) if categories else None,
            "intersection_count": len(bucket["intersections"]),
            "intersection_names": bucket["intersections"],
            "vehicle_volume": bucket["vehicle_volume"],
            "vehicle_volume_per_month": round(bucket["vehicle_volume"] / bucket["vehicle_months"])
            if bucket["vehicle_months"] else None,
        })
    results.sort(key=lambda zone: zone["poi_total"], reverse=True)

    return {
        "zones": results,
        "outside": outside,
        "method": (
            "แบ่งเมืองพัทยาเป็นย่านตามจุดศูนย์กลางและรัศมีที่กำหนดไว้ล่วงหน้า "
            "แล้วนับสถานที่จาก OpenStreetMap และปริมาณรถรายแยกเข้าย่านที่ศูนย์กลางใกล้ที่สุด"
        ),
        "caveats": [
            "ขอบเขตย่านเป็นวงกลมโดยประมาณจากพิกัด OpenStreetMap ไม่ใช่ขอบเขตการปกครองตามกฎหมาย",
            "ความหนาแน่นคิดจากพื้นที่วงกลมเต็มวง ย่านที่มีพื้นที่ทะเลอยู่ในวงจะได้ค่าต่ำกว่าความเป็นจริง",
            f"แยกที่ไม่มีพิกัดหรืออยู่นอกย่านที่กำหนด {outside['intersections']} แยก ไม่ถูกนับให้ย่านใด",
        ],
        "sources": [
            "OpenStreetMap (Overpass API)",
            "Open Data เมืองพัทยา ชุดข้อมูลปริมาณรถ (112)",
        ],
    }


# ---------------------------------------------------------------- แนวโน้ม


@app.get("/api/trends")
def trends(_: dict = Depends(auth.require("view:overview"))) -> list[dict]:
    """จำนวนนักท่องเที่ยวรายเดือนจาก Open Data เมืองพัทยา พร้อมจำนวนวันหยุดในเดือนนั้น"""
    rows = query(
        """SELECT to_char(make_date(calendar_year, month_id, 1), 'YYYY-MM-DD') AS date,
                  calendar_year, month_id, month_name, number_of_tourists AS visitors
           FROM tourism_monthly ORDER BY calendar_year, month_id"""
    )
    holidays_by_month: dict[str, int] = {}
    for row in query(
        f"""SELECT to_char(date, 'YYYY-MM') AS ym, COUNT(*) AS total
            FROM public_holidays WHERE {PUBLIC_HOLIDAY_ONLY} GROUP BY ym"""
    ):
        holidays_by_month[row["ym"]] = row["total"]
    for row in rows:
        row["holidays"] = holidays_by_month.get(row["date"][:7], 0)
    return rows


@app.get("/api/tourism/monthly")
def monthly_tourism(year: int | None = Query(default=None),
    _: dict = Depends(auth.require("view:overview")),
) -> list[dict]:
    clause = "WHERE calendar_year = %s" if year else ""
    parameters: list[Any] = [year] if year else []
    return query(
        f"""SELECT calendar_year_be, calendar_year, month_id, month_name,
                   number_of_tourists, unit, location, source_file
            FROM tourism_monthly {clause} ORDER BY calendar_year, month_id""",
        parameters,
    )


@app.get("/api/traffic/zones")
def traffic_zones(year: int | None = Query(default=None), month: int | None = Query(default=None),
    _: dict = Depends(auth.require("view:spatial")),
) -> list[dict]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if year:
        clauses.append("calendar_year = %s")
        parameters.append(year)
    if month:
        clauses.append("month_id = %s")
        parameters.append(month)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return query(
        f"""SELECT intersection_name, latitude, longitude, geocode_method,
                   SUM(vehicle_volume) AS vehicle_volume
            FROM zone_traffic_volume {where}
            GROUP BY intersection_name, latitude, longitude, geocode_method
            ORDER BY vehicle_volume DESC""",
        parameters,
    )


@app.get("/api/traffic/trends")
def traffic_trends(intersection_name: str | None = Query(default=None),
    _: dict = Depends(auth.require("view:spatial")),
) -> list[dict]:
    clause = "WHERE intersection_name = %s" if intersection_name else ""
    parameters: list[Any] = [intersection_name] if intersection_name else []
    return query(
        f"""SELECT to_char(make_date(calendar_year, month_id, 1), 'YYYY-MM-DD') AS date,
                   calendar_year, month_id, month_name,
                   SUM(vehicle_volume) AS vehicle_volume,
                   COUNT(DISTINCT intersection_name) AS intersections
            FROM zone_traffic_volume {clause}
            GROUP BY calendar_year, month_id, month_name ORDER BY calendar_year, month_id""",
        parameters,
    )


# ---------------------------------------------------------------- ประชากร / สถานที่ / วันหยุด


@app.get("/api/demographics")
def demographics(
    year: int | None = Query(default=None),
    category: str | None = Query(default=None),
    _: dict = Depends(auth.require("view:demographics")),
) -> list[dict]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if year:
        clauses.append("calendar_year = %s")
        parameters.append(year)
    if category:
        clauses.append("category = %s")
        parameters.append(category)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return query(
        f"""SELECT calendar_year_be, calendar_year, category, segment, population, unit, location
            FROM demographic_profiles {where}
            ORDER BY calendar_year, category, population DESC""",
        parameters,
    )


@app.get("/api/poi")
def poi(category: str | None = Query(default=None), limit: int = Query(default=200, le=3000),
    _: dict = Depends(auth.require("view:spatial")),
) -> list[dict]:
    clause = "WHERE category = %s" if category else ""
    parameters: list[Any] = [category] if category else []
    parameters.append(limit)
    return query(
        f"""SELECT name, category, address, latitude, longitude, phone, website
            FROM poi_businesses {clause} ORDER BY name LIMIT %s""",
        parameters,
    )


@app.get("/api/poi/categories")
def poi_categories(_: dict = Depends(auth.require("view:spatial"))) -> list[dict]:
    rows = query(
        """SELECT category, COUNT(*) AS total FROM poi_businesses
           GROUP BY category ORDER BY total DESC"""
    )
    for row in rows:
        row["label"] = category_label(row["category"])
    return rows


@app.get("/api/holidays")
def holidays(year: int | None = Query(default=None),
    _: dict = Depends(auth.require("view:overview")),
) -> list[dict]:
    clause = "WHERE EXTRACT(YEAR FROM date) = %s" if year else ""
    parameters: list[Any] = [year] if year else []
    return query(
        f"""SELECT date, name, local_name, holiday_type, country_code
            FROM public_holidays {clause} ORDER BY date""",
        parameters,
    )


@app.get("/api/holidays/upcoming")
def upcoming_holidays(limit: int = Query(default=5, ge=1, le=50),
    _: dict = Depends(auth.require("view:overview")),
) -> list[dict]:
    return query(
        f"""SELECT date, name, local_name, holiday_type,
                   date - {TODAY_BANGKOK} AS days_away
            FROM public_holidays WHERE date >= {TODAY_BANGKOK} ORDER BY date LIMIT %s""",
        [limit],
    )


# ---------------------------------------------------------------- พยากรณ์ / ผลกระทบ

FORECAST_SOURCE = "Open Data เมืองพัทยา ชุดข้อมูลนักท่องเที่ยวเดินทางลงเกาะล้าน (172)"
FORECAST_CAVEATS = [
    "ข้อมูลมีเพียง 48 เดือน (2022-2025) ยังนับว่าน้อยสำหรับการพยากรณ์",
    "ครอบคลุมเฉพาะผู้เดินทางลงเกาะล้าน ไม่ใช่นักท่องเที่ยวทั้งเมืองพัทยา",
    "รูปแบบฤดูกาลยังไม่คงที่เพราะเพิ่งฟื้นจากโควิด ปี 2022 เดือนพีคสูงกว่าค่าเฉลี่ยทั้งปี 69% แต่ปี 2025 เหลือ 18%",
    "ช่วงประมาณกว้าง ใช้วางแผนกำลังคนแบบคร่าว ๆ ได้ แต่ไม่ควรใช้เป็นตัวเลขผูกพัน",
]
AIR_QUALITY_SOURCE = "Open Data เมืองพัทยา ชุดผลการตรวจวัดคุณภาพอากาศ (149)"
BOAT_SOURCE = "Open Data เมืองพัทยา ชุดข้อมูลสถิติการใช้บริการเรือโดยสารและเรือสปีดโบ๊ท (170)"
ISLAND_WASTE_SOURCE = "Open Data เมืองพัทยา ชุดข้อมูลการบริหารจัดการขยะเกาะล้าน"

IMPACT_CAVEATS = [
    "เป็นความสัมพันธ์ ไม่ใช่การพิสูจน์เหตุและผล",
    "ข้อมูลนักท่องเที่ยวเป็นรายเดือน จึงดูผลของฝนรายวันหรือวันหยุดแต่ละวันไม่ได้",
    "นับเฉพาะวันหยุดราชการ วันสำคัญที่ราชการไม่ได้หยุด (ตรุษจีน คริสต์มาส) ไม่ถูกนับเป็นวันหยุด",
    "ฝนเป็นข้อมูลวิเคราะห์ย้อนหลัง (ERA5) ของจุดเดียวกลางเมืองพัทยา ไม่ใช่ค่าเฉลี่ยทั้งเมือง",
    "PM2.5 มีข้อมูลแค่สองปี จึงหักระดับของทั้งปีออกด้วยเพื่อไม่ให้ความต่างระหว่างปีถูกนับเป็นผลของฝุ่น ผลที่ได้ยังใช้ตัดสินใจไม่ได้จนกว่าจะมีข้อมูลหลายปีกว่านี้",
]


@app.get("/api/forecast/tourism")
def tourism_forecast(months: int = Query(default=6, ge=1, le=12),
    _: dict = Depends(auth.require("view:forecast")),
) -> dict:
    """พยากรณ์นักท่องเที่ยวรายเดือน เลือกวิธีจากปีตรวจสอบ และรายงานความแม่นจากปีทดสอบที่ไม่ถูกใช้เลือกวิธี"""
    labels, series = monthly_tourism_series()
    if not series:
        return {"error": "ยังไม่มีข้อมูลนักท่องเที่ยวในฐานข้อมูล", "caveats": FORECAST_CAVEATS}
    result = cached_analysis(
        f"forecast:{months}:{len(series)}:{labels[-1]}",
        lambda: analytics.forecast_series(series, months, analytics.ml_forecasters(labels[0], holiday_days_by_month())),
    )
    if "error" in result:
        return {"error": result["error"], "caveats": FORECAST_CAVEATS}

    indices = analytics.seasonal_indices(series)
    first_month = int(labels[0][5:7])
    profile = sorted(
        ({"month_id": (first_month - 1 + position) % 12 + 1, "index": round(value, 3)}
         for position, value in enumerate(indices)),
        key=lambda item: item["month_id"],
    )
    # พยากรณ์เริ่มนับจากเดือนถัดจากข้อมูลล่าสุด ไม่ใช่เดือนปัจจุบัน ถ้าข้อมูลค้างจะเห็นได้จากค่านี้
    lag = months_behind_today(labels[-1])
    caveats = list(FORECAST_CAVEATS)
    if lag >= 2:
        caveats.insert(0, f"ข้อมูลล่าสุดคือเดือน {labels[-1]} ช้ากว่าปัจจุบัน {lag} เดือน พยากรณ์จึงเริ่มจากเดือนถัดจากนั้น")
    return {
        "history": [{"month": month, "visitors": round(value)} for month, value in zip(labels, series)],
        "forecast": [{"month": shift_month(labels[-1], point["step"]), **point} for point in result["points"]],
        "model": result["model"],
        "alternative": {
            **{key: value for key, value in result["alternative"].items() if key != "points"},
            "forecast": [{"month": shift_month(labels[-1], point["step"]), "visitors": point["visitors"]}
                         for point in result["alternative"]["points"]],
        } if result["alternative"] else None,
        "comparison": result["comparison"],
        "accuracy": result["accuracy"],
        "seasonal_profile": profile,
        "data": {"last_month": labels[-1], "months_behind_today": lag, "months_available": len(series)},
        "caveats": caveats,
        "source": FORECAST_SOURCE,
    }


def monthly_rain() -> dict[str, float]:
    """ปริมาณฝนรวมรายเดือน (มม.) คีย์เป็นป้ายเดือน YYYY-MM"""
    return {row["month"]: float(row["rain_mm"] or 0) for row in query(
        """SELECT to_char(date, 'YYYY-MM') AS month, SUM(precipitation_mm) AS rain_mm
           FROM weather_daily GROUP BY 1"""
    )}


def holiday_days_by_month() -> dict[str, int]:
    """จำนวนวันหยุดราชการรายเดือน คีย์เป็นป้ายเดือน YYYY-MM"""
    return {row["month"]: row["days"] for row in query(
        f"""SELECT to_char(date, 'YYYY-MM') AS month, COUNT(*) AS days
            FROM public_holidays WHERE {PUBLIC_HOLIDAY_ONLY} GROUP BY 1"""
    )}


def impact_analysis() -> dict:
    """วัดความสัมพันธ์ของฝนและวันหยุดกับจำนวนนักท่องเที่ยว หลังตัดฤดูกาลและแนวโน้มออกแล้ว"""
    labels, series = monthly_tourism_series()
    if len(series) < 2 * analytics.MONTHS_IN_YEAR:
        return {"error": "ข้อมูลนักท่องเที่ยวน้อยกว่า 24 เดือน ยังวิเคราะห์ผลกระทบไม่ได้", "caveats": IMPACT_CAVEATS}

    visitor_index = analytics.deseasonalized_index(series)
    rain_by_month = monthly_rain()
    holidays_by_month = holiday_days_by_month()

    rain_months = [(month, index) for month, index in zip(labels, visitor_index) if month in rain_by_month]
    rain = analytics.rain_impact(
        [index for _, index in rain_months],
        [rain_by_month[month] for month, _ in rain_months],
        [month for month, _ in rain_months],
    ) if len(rain_months) >= analytics.MONTHS_IN_YEAR else None

    # วันหยุดเริ่มมีข้อมูลปี 2024 จึงตัดเฉพาะช่วงที่มีข้อมูลจริง ไม่นับเดือนที่ไม่มีข้อมูลว่า "ไม่มีวันหยุด"
    first_holiday_month = min(holidays_by_month) if holidays_by_month else None
    holiday_months = [
        (month, index) for month, index in zip(labels, visitor_index)
        if first_holiday_month and month >= first_holiday_month
    ]
    holidays = analytics.holiday_impact(
        [index for _, index in holiday_months],
        [holidays_by_month.get(month, 0) for month, _ in holiday_months],
        [month for month, _ in holiday_months],
    ) if len(holiday_months) >= analytics.MONTHS_IN_YEAR else None

    pm25_by_month = {row["month"]: row["pm25_avg"] for row in query(
        """SELECT to_char(make_date(calendar_year, month_id, 1), 'YYYY-MM') AS month, AVG(pm25_avg) AS pm25_avg
           FROM air_quality_monthly GROUP BY 1"""
    )}
    pm25_months = [(month, index) for month, index in zip(labels, visitor_index) if month in pm25_by_month]
    # ต้องมีอย่างน้อยสองปี ถ้ามีปีเดียว ค่าปกติของเดือนจะเท่ากับค่าของเดือนนั้นเอง ส่วนต่างเป็น 0 ทุกเดือน 
    pm25 = analytics.pm25_impact(
        [index for _, index in pm25_months],
        [pm25_by_month[month] for month, _ in pm25_months],
        [month for month, _ in pm25_months],
    ) if len(pm25_months) >= 2 * analytics.MONTHS_IN_YEAR else None

    return {
        "method": (
            "ตัดฤดูกาล (ratio-to-moving-average) และแนวโน้มเชิงเส้นออกจากจำนวนนักท่องเที่ยวก่อน "
            "เหลือเป็นดัชนี 1.0 = เท่าที่ควรจะเป็น แล้ววัดความสัมพันธ์กับฝนและ PM2.5 ที่มากกว่าปกติของเดือนนั้น และจำนวนวันหยุด"
        ),
        "rain": rain,
        "holidays": holidays,
        "pm25": pm25,
        "caveats": IMPACT_CAVEATS,
        "sources": [FORECAST_SOURCE, "Open-Meteo Archive (ERA5)", HOLIDAY_SOURCE, AIR_QUALITY_SOURCE],
    }


@app.get("/api/impact")
def impact(_: dict = Depends(auth.require("view:forecast"))) -> dict:
    labels, series = monthly_tourism_series()
    return cached_analysis(f"impact:{len(series)}:{labels[-1] if labels else '-'}", impact_analysis)


# ---------------------------------------------------------------- คำแนะนำ

# สมมติฐานของคำแนะนำเรื่องกำลังคนและสต็อก เขียนไว้ตรงนี้ให้เห็นชัดว่าไม่ได้มาจากข้อมูลธุรกิจจริง
# เพราะฐานข้อมูลไม่มีข้อมูลร้านค้า พนักงาน หรือยอดขาย
STAFFING_ASSUMPTION = "สมมติว่ากำลังคนที่ต้องใช้แปรผันตรงกับจำนวนนักท่องเที่ยว"
STOCK_ASSUMPTION = "สมมติว่าสต็อกที่ต้องเตรียมแปรผันตรงกับจำนวนนักท่องเที่ยว และเผื่อของขาดไว้ที่ขอบบนของช่วงประมาณ"
# ต่ำกว่านี้ถือว่าเปลี่ยนแปลงน้อยกว่าความคลาดเคลื่อนของแบบจำลอง ไม่ควรสั่งปรับกำลังคน
MATERIAL_CHANGE_PERCENT = 5


def _weather_signal() -> Optional[dict]:
    try:
        forecast = weather_forecast(days=7)
    except Exception:  # ไม่มีเน็ต/Open-Meteo ล่ม ให้ข้ามสัญญาณอากาศไปแทนที่จะพังทั้งหน้า
        return None
    rainy = [day for day in forecast if (day["precipitation_probability_percent"] or 0) >= 60]
    holidays_ahead = [day for day in forecast if day["holiday"]]
    return {"forecast": forecast, "rainy_days": rainy, "holidays_ahead": holidays_ahead}


@app.get("/api/recommendations")
def recommendations(_: dict = Depends(auth.require("view:recommendations"))) -> list[dict]:
    """คำแนะนำที่อนุมานจากสัญญาณข้อมูลจริง ทุกข้อระบุตัวเลขและแหล่งที่มาที่ใช้ตัดสิน

    key คือชื่อคงที่ของกฎแต่ละข้อ (หัวข้อเปลี่ยนตามข้อมูล) ใช้จับคู่กับลำดับที่ AI จัดไว้
    """
    results: list[dict] = []

    forecast = tourism_forecast(months=6)
    if forecast.get("forecast"):
        next_month = forecast["forecast"][0]
        history = {row["month"]: row["visitors"] for row in forecast["history"]}
        accuracy = forecast["accuracy"]
        model_source = (
            f"แบบจำลอง{forecast['model']['label']} คลาดเคลื่อนในปีทดสอบ {accuracy['test_mape_percent']}% "
            f"(วิธีพื้นฐาน {accuracy['seasonal_naive_mape_percent']}%)"
        )
        baseline = history.get(shift_month(next_month["month"], -12))
        if baseline:
            change = (next_month["visitors"] / baseline - 1) * 100
            action = (
                f"ควร{'เพิ่ม' if change > 0 else 'ลด'}กำลังคนราว {abs(change):.0f}%"
                if abs(change) >= MATERIAL_CHANGE_PERCENT
                else "ยังไม่ต้องปรับกำลังคน เพราะเปลี่ยนแปลงน้อยกว่าความคลาดเคลื่อนของแบบจำลอง"
            )
            results.append({
                "key": "staffing",
                "title": f"วางแผนกำลังคนเดือน{thai_month(next_month['month'])} (เดือนถัดจากข้อมูลล่าสุด)",
                "detail": (
                    f"พยากรณ์ {next_month['visitors']:,} คน เทียบเดือนเดียวกันปีก่อนที่ {baseline:,} คน "
                    f"({change:+.1f}%) {action}"
                ),
                "signal": "พยากรณ์นักท่องเที่ยว",
                "assumption": STAFFING_ASSUMPTION,
                "source": model_source,
            })
        if next_month.get("upper"):
            buffer_percent = (next_month["upper"] / next_month["visitors"] - 1) * 100
            results.append({
                "key": "stock",
                "title": f"เตรียมสต็อกเดือน{thai_month(next_month['month'])} ให้รองรับถึง {next_month['upper']:,} คน",
                "detail": (
                    f"ค่าพยากรณ์อยู่ที่ {next_month['visitors']:,} คน แต่ขอบบนของช่วงประมาณ 95% คือ "
                    f"{next_month['upper']:,} คน การเผื่อสต็อกอีก {buffer_percent:.0f}% จะกันของขาดกรณีคนมากกว่าคาด"
                ),
                "signal": "ช่วงความไม่แน่นอน",
                "assumption": STOCK_ASSUMPTION,
                "source": model_source,
            })
        profile = {item["month_id"]: item["index"] for item in forecast["seasonal_profile"]}
        if profile:
            peak_month = max(profile, key=lambda month_id: profile[month_id])
            results.append({
                "key": "season_peak",
                "title": f"เดือน{THAI_MONTH_NAMES[peak_month - 1]}เป็นเดือนที่คนมากที่สุดตามรูปฤดูกาล",
                "detail": (
                    f"เฉลี่ย 4 ปีที่ผ่านมา สูงกว่าค่าเฉลี่ยทั้งปี {(profile[peak_month] - 1) * 100:.0f}% "
                    "ควรวางแผนกำลังคนและสต็อกล่วงหน้าก่อนถึงเดือนนี้"
                ),
                "signal": "รูปแบบฤดูกาล",
                "source": FORECAST_SOURCE,
            })

    lag = forecast.get("data", {}).get("months_behind_today", 0)
    if lag >= 2:
        results.append({
            "key": "stale_data",
            "title": f"ข้อมูลนักท่องเที่ยวค้างอยู่ที่เดือน{thai_month(forecast['data']['last_month'])}",
            "detail": (
                f"ช้ากว่าปัจจุบัน {lag} เดือน คำแนะนำทั้งหมดที่อิงการพยากรณ์จึงเป็นการต่อยอดจากข้อมูลเก่า "
                "ควรนำเข้าข้อมูลเดือนล่าสุดจาก Open Data เมืองพัทยาก่อนนำไปใช้ตัดสินใจจริง"
            ),
            "signal": "คุณภาพข้อมูล",
            "source": FORECAST_SOURCE,
        })

    analysis = impact()
    rain_effect = (analysis or {}).get("rain")
    if rain_effect and rain_effect.get("correlation") is not None:
        if rain_effect["significant"]:
            detail = (
                f"เมื่อฝนมากกว่าปกติของเดือนนั้น 100 มม. จำนวนคนต่างไปจากที่ควรเป็น {rain_effect['effect_percent']:+.1f}% "
                f"(r = {rain_effect['correlation']}, ช่วงความเชื่อมั่น {rain_effect['correlation_ci_95']}, "
                f"{rain_effect['months_used']} เดือน) ควรเตรียมกิจกรรมในร่มไว้รองรับ"
            )
            title = "เดือนที่ฝนมากกว่าปกติมีคนน้อยกว่าที่ควรเป็น"
        else:
            detail = (
                f"ทดสอบแล้วยังไม่พบความสัมพันธ์ชัดเจน (r = {rain_effect['correlation']}, "
                f"ช่วงความเชื่อมั่นคร่อมศูนย์, {rain_effect['months_used']} เดือน) จึงยังไม่ควรใช้ฝนวางแผนกำลังคน"
            )
            title = "ยังสรุปผลของฝนต่อจำนวนนักท่องเที่ยวไม่ได้"
        results.append({
            "key": "rain_impact",
            "title": title,
            "detail": detail,
            "signal": "ผลกระทบจากฝน",
            "assumption": "เป็นความสัมพันธ์หลังตัดฤดูกาลออกแล้ว ไม่ใช่การพิสูจน์เหตุและผล",
            "source": "Open-Meteo Archive (ERA5) + " + FORECAST_SOURCE,
        })

    busiest = query(
        """SELECT intersection_name, latitude, longitude, SUM(vehicle_volume) AS vehicle_volume
           FROM zone_traffic_volume
           WHERE calendar_year = (SELECT MAX(calendar_year) FROM zone_traffic_volume)
           GROUP BY intersection_name, latitude, longitude
           ORDER BY vehicle_volume DESC LIMIT 3"""
    )
    for row in busiest:
        results.append({
            "key": f"traffic:{row['intersection_name']}",
            "title": f"จัดการจราจรและจุดบริการที่{row['intersection_name']}",
            "detail": f"ปริมาณรถสะสมปีล่าสุด {row['vehicle_volume']:,} คัน สูงติดอันดับต้นของเมือง",
            "signal": "ปริมาณรถสูง",
            "source": "Open Data เมืองพัทยา ชุดข้อมูลปริมาณรถ (112)",
            "latitude": row["latitude"],
            "longitude": row["longitude"],
        })

    peak_month = query_one(
        """SELECT month_name, calendar_year, number_of_tourists FROM tourism_monthly
           WHERE calendar_year = (SELECT MAX(calendar_year) FROM tourism_monthly)
           ORDER BY number_of_tourists DESC LIMIT 1"""
    )
    if peak_month:
        results.append({
            "key": "peak_month",
            "title": f"เตรียมกำลังรับช่วงพีคเดือน{peak_month['month_name']}",
            "detail": (
                f"เดือน{peak_month['month_name']} {peak_month['calendar_year']} "
                f"มีนักท่องเที่ยวลงเกาะล้าน {peak_month['number_of_tourists']:,} คน สูงสุดของปี"
            ),
            "signal": "ฤดูกาลท่องเที่ยว",
            "source": "Open Data เมืองพัทยา ชุดข้อมูลนักท่องเที่ยวเดินทางลงเกาะล้าน (172)",
        })

    weather = _weather_signal()
    if weather and weather["holidays_ahead"]:
        upcoming = weather["holidays_ahead"][0]
        rain = upcoming["precipitation_probability_percent"] or 0
        results.append({
            "key": "holiday_ahead",
            "title": f"วันหยุด{upcoming['holiday']} ({thai_date(upcoming['date'])})",
            "detail": (
                f"พยากรณ์ฝน {rain}% อุณหภูมิสูงสุด {upcoming['temperature_max_c']}°C "
                + ("ควรเตรียมพื้นที่ในร่มและกิจกรรมสำรอง" if rain >= 60 else "อากาศเอื้อต่อกิจกรรมกลางแจ้ง")
            ),
            "signal": "วันหยุดใกล้ถึง",
            "source": f"{HOLIDAY_SOURCE} + Open-Meteo",
        })
    if weather and weather["rainy_days"]:
        days = weather["rainy_days"]
        results.append({
            "key": "rainy_week",
            "title": f"มีวันฝนตกหนัก {len(days)} วันใน 7 วันข้างหน้า",
            "detail": "วันที่ " + ", ".join(thai_date(day["date"]) for day in days) + " ควรสื่อสารกิจกรรมในร่มล่วงหน้า",
            "signal": "สภาพอากาศ",
            "source": "Open-Meteo",
        })

    dense = query(
        """SELECT category, COUNT(*) AS total FROM poi_businesses
           GROUP BY category ORDER BY total DESC LIMIT 1"""
    )
    if dense:
        results.append({
            "key": "business_mix",
            "title": f"ธุรกิจประเภท{category_label(dense[0]['category'])}หนาแน่นที่สุด ({dense[0]['total']:,} แห่ง)",
            "detail": "ใช้เป็นฐานกำหนดมาตรการดูแลนักท่องเที่ยวและการกระจายกิจกรรมกลางคืน",
            "signal": "โครงสร้างธุรกิจ",
            "source": "OpenStreetMap (Overpass API)",
        })
    return results


# ---------------------------------------------------------------- เกาะล้านและสิ่งแวดล้อม

MONTH_LABEL = "to_char(make_date(calendar_year, month_id, 1), 'YYYY-MM')"


def tourists_by_month() -> dict[str, float]:
    return dict(zip(*monthly_tourism_series()))


@app.get("/api/island/boats")
def island_boats(_: dict = Depends(auth.require("view:overview"))) -> dict:
    """ผู้โดยสารเรือไปเกาะล้านรายเดือน แยกประเภทเรือ เทียบกับจำนวนนักท่องเที่ยว และสัดส่วนรายเส้นทาง"""
    tourists = tourists_by_month()
    months: dict[str, dict] = {}
    for row in query(
        f"""SELECT {MONTH_LABEL} AS month, boat_type_name,
                   SUM(number_of_passengers) AS passengers, SUM(number_of_trips) AS trips
            FROM boat_passengers_monthly GROUP BY 1, 2 ORDER BY 1, 2"""
    ):
        month = months.setdefault(row["month"], {
            "month": row["month"], "passengers": 0, "trips": 0, "by_type": {}, "tourists": tourists.get(row["month"]),
        })
        month["passengers"] += row["passengers"]
        month["trips"] += row["trips"]
        month["by_type"][row["boat_type_name"]] = row["passengers"]

    routes = query(
        """SELECT route_name, boat_type_name, SUM(number_of_passengers) AS passengers, SUM(number_of_trips) AS trips
           FROM boat_passengers_monthly GROUP BY 1, 2 ORDER BY passengers DESC"""
    )
    total = sum(route["passengers"] for route in routes) or 1
    for route in routes:
        route["share_percent"] = round(route["passengers"] / total * 100, 1)
        route["passengers_per_trip"] = round(route["passengers"] / route["trips"]) if route["trips"] else None
    return {
        "months": list(months.values()),
        "routes": routes,
        "caveats": [
            "นับผู้โดยสารทุกเที่ยว ทั้งขาไปและขากลับ ยอดจึงสูงกว่าจำนวนนักท่องเที่ยวและเทียบกันตรง ๆ ไม่ได้ ให้ดูทิศทางการขึ้นลง",
        ],
        "source": BOAT_SOURCE,
    }


@app.get("/api/island/waste")
def island_waste(_: dict = Depends(auth.require("view:overview"))) -> dict:
    """ขยะเกาะล้านรายเดือน เทียบกับจำนวนนักท่องเที่ยว"""
    tourists = tourists_by_month()
    months: dict[str, dict] = {}
    for row in query(
        f"""SELECT {MONTH_LABEL} AS month, waste_type, SUM(waste_amount_ton) AS tons
            FROM island_waste_monthly GROUP BY 1, 2 ORDER BY 1, 2"""
    ):
        months.setdefault(row["month"], {"month": row["month"], "by_type": {}})["by_type"][row["waste_type"]] = round(row["tons"], 2)
    result = []
    for month in months.values():
        # "ขยะทั่วไป" คือปริมาณที่เก็บได้ ส่วน "ลงบ่อ" คือส่วนที่นำไปฝังกลบ เป็นขยะก้อนเดียวกัน ห้ามบวกกัน
        collected = month["by_type"].get("ขยะทั่วไป")
        visitors = tourists.get(month["month"])
        result.append({
            **month,
            "collected_ton": collected,
            "tourists": visitors,
            "kg_per_tourist": round(collected * 1000 / visitors, 2) if collected is not None and visitors else None,
        })
    paired = [row for row in result if row["collected_ton"] is not None and row["tourists"]]
    r = analytics.correlation([row["tourists"] for row in paired], [row["collected_ton"] for row in paired]) if len(paired) >= 4 else None
    interval = analytics.correlation_interval(r, len(paired)) if r is not None else None
    return {
        "months": result,
        "correlation": {
            "r": round(r, 3),
            "ci_95": [round(interval[0], 3), round(interval[1], 3)] if interval else None,
            "months_used": len(paired),
        } if r is not None else None,
        "caveats": [
            "ขยะบนเกาะมาจากทั้งผู้อยู่อาศัยและนักท่องเที่ยว กิโลกรัมต่อนักท่องเที่ยวจึงเป็นตัวชี้วัดภาระของเกาะ ไม่ใช่ขยะที่นักท่องเที่ยวหนึ่งคนทิ้ง",
        ],
        "source": ISLAND_WASTE_SOURCE,
    }


@app.get("/api/air-quality")
def air_quality(_: dict = Depends(auth.require("view:overview"))) -> dict:
    return {
        "months": query(
            f"""SELECT {MONTH_LABEL} AS month, pm25_avg, pm25_min, pm25_max, exceed_days
                FROM air_quality_monthly ORDER BY calendar_year, month_id"""
        ),
        "source": AIR_QUALITY_SOURCE,
    }


# ---------------------------------------------------------------- พฤติกรรมการเดินทาง

MAINLAND_PIER = "พัทยา"
REVENUE_SOURCE = "Open Data เมืองพัทยา ชุดข้อมูลรายได้จากการจัดเก็บภาษีและค่าธรรมเนียมต่าง ๆ (91)"


@app.get("/api/behavior")
def travel_behavior(_: dict = Depends(auth.require("view:overview"))) -> dict:
    """รูปแบบการเดินทางของนักท่องเที่ยวเกาะล้าน: เดินทางช่วงไหนของปี ขึ้นลงท่าไหน และเลือกเรือประเภทใด

    เป็นพฤติกรรมระดับกลุ่มที่อ่านได้จากยอดรวมรายเดือน ไม่ใช่การติดตามนักท่องเที่ยวรายคน
    """
    labels, series = monthly_tourism_series()
    seasonality = []
    if len(series) >= 2 * analytics.MONTHS_IN_YEAR:
        first_month = int(labels[0][5:7])
        seasonality = sorted(
            ({"month_id": (first_month - 1 + position) % 12 + 1, "index": round(value, 3)}
             for position, value in enumerate(analytics.seasonal_indices(series))),
            key=lambda item: item["month_id"],
        )

    routes = query(
        """SELECT route_name, boat_type_name, SUM(number_of_passengers) AS passengers, SUM(number_of_trips) AS trips
           FROM boat_passengers_monthly GROUP BY 1, 2"""
    )
    # เส้นทางตั้งชื่อเป็น "ต้นทาง-ปลายทาง" จึงแยกได้ว่าแต่ละท่าบนเกาะมีคนขึ้นเกาะ (ขาไป) และออกจากเกาะ (ขากลับ) เท่าไร
    piers: dict[str, dict] = {}
    for route in routes:
        ends = [part.strip() for part in route["route_name"].split("-")]
        if len(ends) != 2 or MAINLAND_PIER not in ends or ends[0] == ends[1]:
            continue
        island_pier = ends[1] if ends[0] == MAINLAND_PIER else ends[0]
        pier = piers.setdefault(island_pier, {"pier": island_pier, "arrivals": 0, "departures": 0})
        pier["arrivals" if ends[0] == MAINLAND_PIER else "departures"] += route["passengers"]
    pier_list = sorted(piers.values(), key=lambda pier: -(pier["arrivals"] + pier["departures"]))

    types: dict[str, dict] = {}
    for route in routes:
        boat = types.setdefault(route["boat_type_name"], {"boat_type_name": route["boat_type_name"], "passengers": 0, "trips": 0})
        boat["passengers"] += route["passengers"]
        boat["trips"] += route["trips"]
    total = sum(boat["passengers"] for boat in types.values()) or 1
    for boat in types.values():
        boat["share_percent"] = round(boat["passengers"] / total * 100, 1)
        boat["passengers_per_trip"] = round(boat["passengers"] / boat["trips"]) if boat["trips"] else None

    insights = []
    if seasonality:
        peak = max(seasonality, key=lambda item: item["index"])
        low = min(seasonality, key=lambda item: item["index"])
        insights.append(
            f"นักท่องเที่ยวมาเกาะล้านมากที่สุดในเดือน{THAI_MONTH_NAMES[peak['month_id'] - 1]} "
            f"สูงกว่าค่าเฉลี่ยทั้งปี {(peak['index'] - 1) * 100:.0f}% "
            f"และน้อยที่สุดในเดือน{THAI_MONTH_NAMES[low['month_id'] - 1]} ต่ำกว่าค่าเฉลี่ย {(1 - low['index']) * 100:.0f}%"
        )
    for pier in pier_list:
        if pier["arrivals"] and pier["departures"]:
            gap = (pier["departures"] / pier["arrivals"] - 1) * 100
            insights.append(
                f"ท่า{pier['pier']}: ขึ้นเกาะที่ท่านี้ {pier['arrivals']:,} คน ออกจากเกาะที่ท่านี้ {pier['departures']:,} คน "
                f"คนออก{'มากกว่า' if gap > 0 else 'น้อยกว่า'}คนขึ้น {abs(gap):.0f}%"
            )
    arrivals = sum(pier["arrivals"] for pier in pier_list)
    departures = sum(pier["departures"] for pier in pier_list)
    if arrivals and departures:
        insights.append(
            f"รวมทุกท่า เรือโดยสารขากลับมีผู้โดยสาร {departures:,} คน {'มากกว่า' if departures > arrivals else 'น้อยกว่า'}ขาไป "
            f"{arrivals:,} คน อยู่ {abs(departures / arrivals - 1) * 100:.0f}%"
        )
    for boat in sorted(types.values(), key=lambda boat: -boat["passengers"]):
        insights.append(
            f"{boat['boat_type_name']}มีผู้โดยสาร {boat['share_percent']}% ของทั้งหมด"
            + (f" เฉลี่ย {boat['passengers_per_trip']:,} คนต่อเที่ยว" if boat["passengers_per_trip"] else "")
        )
    return {
        "seasonality": seasonality,
        "piers": pier_list,
        "boat_types": sorted(types.values(), key=lambda boat: -boat["passengers"]),
        "insights": insights,
        "caveats": [
            "เป็นพฤติกรรมระดับกลุ่มจากยอดรวมรายเดือน ไม่ได้ติดตามนักท่องเที่ยวรายคน จึงบอกไม่ได้ว่าใครไปไหนหรืออยู่นานเท่าไร",
            "ส่วนต่างระหว่างขาไปกับขากลับอาจมาจากคนที่ไปด้วยเรือเช่าเหมาลำแล้วกลับด้วยเรือโดยสาร หรือจากการบันทึกสองขาที่ไม่ครบเท่ากัน ข้อมูลนี้แยกสองสาเหตุไม่ได้",
        ],
        "sources": [FORECAST_SOURCE, BOAT_SOURCE],
    }


@app.get("/api/economy/revenue")
def city_revenue(_: dict = Depends(auth.require("view:overview"))) -> dict:
    """รายได้ของเมืองพัทยารายปีงบประมาณ แยกหมวด และรายการที่มากที่สุดของปีล่าสุด"""
    by_category = query(
        """SELECT budget_year, category_name, SUM(amount_baht) AS amount_baht
           FROM city_revenue_yearly GROUP BY 1, 2 ORDER BY 1, 3 DESC"""
    )
    years = sorted({row["budget_year"] for row in by_category})
    totals = {year: sum(row["amount_baht"] for row in by_category if row["budget_year"] == year) for year in years}
    items = []
    if years:
        previous = {row["income_name"]: row["amount_baht"] for row in query(
            "SELECT income_name, SUM(amount_baht) AS amount_baht FROM city_revenue_yearly WHERE budget_year = %s GROUP BY 1",
            [years[-1] - 1],
        )}
        for row in query(
            """SELECT income_name, category_name, SUM(amount_baht) AS amount_baht FROM city_revenue_yearly
               WHERE budget_year = %s GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 8""",
            [years[-1]],
        ):
            before = previous.get(row["income_name"])
            items.append({**row, "change_percent": round((row["amount_baht"] / before - 1) * 100, 1) if before else None})
    return {
        "years": [
            {
                "budget_year": year,
                "total_baht": round(totals[year]),
                "change_percent": round((totals[year] / totals[year - 1] - 1) * 100, 1) if totals.get(year - 1) else None,
                "by_category": {row["category_name"]: round(row["amount_baht"]) for row in by_category if row["budget_year"] == year},
            }
            for year in years
        ],
        "top_items": items,
        "caveats": [
            "เป็นรายได้ของเทศบาลเมืองพัทยารายปีงบประมาณ (ต.ค.–ก.ย.) ไม่ใช่รายได้จากการท่องเที่ยวของเมือง และเทียบรายเดือนกับจำนวนนักท่องเที่ยวไม่ได้",
            "รวมภาษีที่รัฐจัดสรรให้และเงินอุดหนุนทั่วไป ซึ่งไม่ได้ขึ้นกับเศรษฐกิจในเมืองโดยตรง",
        ],
        "source": REVENUE_SOURCE,
    }


# ---------------------------------------------------------------- ธุรกิจของฉัน


def business_rows(owner_id: int) -> list[dict]:
    return query(
        """SELECT calendar_year AS year, month_id AS month, revenue_baht AS revenue, customers
           FROM business_monthly WHERE owner_id = %s ORDER BY calendar_year, month_id""",
        [owner_id],
    )


@app.get("/api/business")
def business_analysis(user: dict = Depends(auth.require("manage:business"))) -> dict:
    """ผลวิเคราะห์ธุรกิจของผู้ใช้ที่ล็อกอินอยู่เท่านั้น ไม่มีทางขอดูของคนอื่น แม้เป็นผู้ดูแลระบบ"""
    profile = query_one(
        """SELECT name, is_sample, source_file, ai_summary,
                  to_char(uploaded_at AT TIME ZONE 'Asia/Bangkok', 'YYYY-MM-DD HH24:MI') AS uploaded_at,
                  to_char(ai_created_at AT TIME ZONE 'Asia/Bangkok', 'YYYY-MM-DD HH24:MI') AS ai_created_at
           FROM businesses WHERE owner_id = %s""",
        [user["id"]],
    )
    rows = business_rows(user["id"]) if profile else []
    if not rows:
        return {"business": None, "template_header": business.TEMPLATE_HEADER}
    stored = profile.pop("ai_summary")
    ai_created_at = profile.pop("ai_created_at")
    labels, series = monthly_tourism_series()
    visitor_index = dict(zip(labels, analytics.deseasonalized_index(series))) if len(series) >= 2 * analytics.MONTHS_IN_YEAR else {}
    today = datetime.now(timezone(timedelta(hours=7)))
    # การเลือกวิธีพยากรณ์แบบเลื่อนจุดตัดใช้เวลาหลายวินาที เก็บผลไว้จนกว่าจะอัปโหลดข้อมูลชุดใหม่หรือข้อมูลเมืองเปลี่ยน
    analysis = cached_analysis(
        f"business:{user['id']}:{profile['uploaded_at']}:{len(rows)}:{len(series)}:{today:%Y-%m}",
        lambda: business.analyze(rows, dict(zip(labels, series)), visitor_index, monthly_rain(), holiday_days_by_month(), f"{today:%Y-%m}"),
    )
    return {
        "business": profile,
        "ai_enabled": bool(ai_summary.api_key()),
        "ai": {**json.loads(stored), "created_at": ai_created_at} if stored else None,
        **analysis,
        "caveats": [
            "นักท่องเที่ยวที่ใช้เทียบคือผู้เดินทางลงเกาะล้าน ไม่ใช่นักท่องเที่ยวทั้งเมืองพัทยา",
            "ความสัมพันธ์วัดหลังตัดฤดูกาลและแนวโน้มออกแล้ว และไม่ใช่การพิสูจน์เหตุและผล",
            "ข้อมูลธุรกิจถูกส่งให้ AI เฉพาะเมื่อเจ้าของกดปุ่มให้ AI สรุปเอง และไม่อยู่ในบทสรุปผู้บริหารของเมือง",
        ],
    }


@app.post("/api/business")
async def business_upload(request: Request, filename: str = Query(...), name: str = Query(...),
    sample: bool = Query(default=False),
    user: dict = Depends(auth.require("manage:business")),
) -> dict:
    """อัปโหลดไฟล์รายได้รายเดือน แทนที่ข้อมูลเดิมของผู้ใช้คนนี้ทั้งชุด ไฟล์ที่มีแถวผิดจะไม่ถูกบันทึกเลย"""
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="ต้องระบุชื่อธุรกิจ")
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ .csv")
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="ไฟล์ใหญ่เกินไป")
    data = await request.body()
    try:
        rows = business.parse_upload(data, filename)
    except import_csv.RowErrors as exc:
        return {"ok": False, "errors": [{"row_number": line, "error_text": text} for line, text in exc.errors[:50]]}
    except ValueError as exc:  # ถอดรหัสตัวอักษรของไฟล์ไม่ได้
        raise HTTPException(status_code=400, detail=str(exc))

    def store() -> None:
        with get_pool().connection() as connection, connection.transaction():
            connection.execute("DELETE FROM businesses WHERE owner_id = %s", (user["id"],))
            connection.execute(
                "INSERT INTO businesses (owner_id, name, is_sample, source_file) VALUES (%s, %s, %s, %s)",
                (user["id"], name, sample, Path(filename.replace("\\", "/")).name),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO business_monthly (owner_id, calendar_year, month_id, revenue_baht, customers) VALUES (%s, %s, %s, %s, %s)",
                    [(user["id"], row["year"], row["month"], row["revenue"], row["customers"]) for row in rows],
                )
    await run_in_threadpool(store)
    # บันทึกแค่ว่ามีการอัปโหลดและกี่เดือน ไม่บันทึกตัวเลขของธุรกิจลงประวัติที่ผู้ดูแลระบบอ่านได้
    audit("business_upload", user["username"], details=f"{len(rows)} เดือน{' · ข้อมูลสมมติ' if sample else ''}")
    return {"ok": True, "months": len(rows), "errors": []}


@app.post("/api/business/ai")
def business_ai(user: dict = Depends(auth.require("manage:business"))) -> dict:
    """ให้ AI เรียบเรียงบทสรุปและจัดลำดับคำแนะนำของธุรกิจนี้ เรียกเมื่อเจ้าของข้อมูลกดปุ่มเองเท่านั้น

    ข้อมูลที่ส่งออกไปคือข้อเท็จจริงสรุป (ยอดรวม พยากรณ์ ผลทางสถิติ) ไม่ใช่ตารางรายเดือนทั้งชุด
    """
    analysis = business_analysis(user=user)
    if not analysis["business"]:
        raise HTTPException(status_code=400, detail="ยังไม่มีข้อมูลธุรกิจ")
    facts = business.facts_for_ai(analysis["business"]["name"], analysis)
    options = {item["key"]: f"{item['title']} — {item['detail']}" for item in analysis["recommendations"]}
    try:
        result = ai_summary.generate(facts, options, ai_summary.BUSINESS_AUDIENCE)
    except ai_summary.SummaryError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    written = " ".join([result["content"], *(item["reason"] for item in result["ranking"])])
    stored = {
        "content": result["content"], "ranking": result["ranking"], "model": result["model"],
        "unverified_numbers": ai_summary.unverified_numbers(written, facts + "\n" + "\n".join(options.values())),
    }
    query(
        "UPDATE businesses SET ai_summary = %s, ai_created_at = now() WHERE owner_id = %s",
        [json.dumps(stored, ensure_ascii=False), user["id"]],
    )
    audit("business_ai", user["username"], result["model"])
    return business_analysis(user=user)


@app.delete("/api/business")
def business_delete(user: dict = Depends(auth.require("manage:business"))) -> dict:
    query("DELETE FROM businesses WHERE owner_id = %s", [user["id"]])
    audit("business_delete", user["username"])
    return {"ok": True}


# ---------------------------------------------------------------- สรุปโดย AI


def thai_month(label: str) -> str:
    return f"{THAI_MONTH_NAMES[int(label[5:7]) - 1]} {label[:4]}"


def summary_facts() -> str:
    """ข้อเท็จจริงที่ส่งให้ AI เรียบเรียง ทุกตัวเลขคำนวณโดยระบบและจัดรูปแบบไว้แล้ว AI ต้องคัดลอกไปตรง ๆ"""
    verdict = lambda result: "ชัดเจนทางสถิติ" if result["significant"] else "ยังสรุปไม่ได้ทางสถิติ"
    facts: list[str] = []

    tourism = summary(_={})["tourism"]
    if tourism["tourists_year_total"]:
        change = tourism["year_over_year_percent"]
        facts.append(
            f"นักท่องเที่ยวลงเกาะล้านปี {tourism['year']} รวม {tourism['tourists_year_total']:,} คน"
            + (f" เปลี่ยนแปลง {change:+}% เทียบปีก่อนหน้า" if change is not None else "")
        )

    forecast = tourism_forecast(months=3, _={})
    if "error" not in forecast:
        data = forecast["data"]
        facts.append(
            f"ข้อมูลนักท่องเที่ยวล่าสุดคือเดือน{thai_month(data['last_month'])} เก่ากว่าปัจจุบัน {data['months_behind_today']} เดือน"
        )
        for point in forecast["forecast"]:
            facts.append(
                f"พยากรณ์เดือน{thai_month(point['month'])}: {point['visitors']:,} คน "
                f"(ช่วงประมาณ 95% คือ {point['lower']:,} ถึง {point['upper']:,} คน)"
            )
        facts.append(f"แบบจำลองพยากรณ์คลาดเคลื่อน {forecast['accuracy']['test_mape_percent']}% ในปีทดสอบ")

    impact = impact_analysis()
    for key, label in (
        ("rain", "เดือนที่ฝนมากกว่าปกติ 100 มม."),
        ("holidays", "เดือนที่วันหยุดราชการเพิ่มขึ้น 1 วัน"),
        ("pm25", "เดือนที่ PM2.5 สูงกว่าปกติ 10 มคก./ลบ.ม."),
    ):
        result = impact.get(key)
        if result:
            facts.append(
                f"{label} นักท่องเที่ยวต่างจากที่คาด {result['effect_percent']:+}% "
                f"ความสัมพันธ์นี้{verdict(result)} (ข้อมูล {result['months_used']} เดือน)"
            )

    boats = island_boats(_={})
    if boats["routes"]:
        top = boats["routes"][0]
        facts.append(
            f"เส้นทางเรือที่มีผู้โดยสารมากที่สุดคือ {top['route_name']} คิดเป็น {top['share_percent']}% ของผู้โดยสารเรือทั้งหมด"
            f" (นับทุกเที่ยวทั้งขาไปและขากลับ ช่วง {thai_month(boats['months'][0]['month'])} ถึง {thai_month(boats['months'][-1]['month'])})"
        )

    waste = island_waste(_={})
    if waste["correlation"]:
        stat = waste["correlation"]
        interval = stat["ci_95"]
        clear = bool(interval and (interval[0] > 0 or interval[1] < 0))
        facts.append(
            f"ปริมาณขยะบนเกาะล้านขึ้นลงตามจำนวนนักท่องเที่ยว (r = {stat['r']} จาก {stat['months_used']} เดือน) "
            f"ความสัมพันธ์นี้{'ชัดเจนทางสถิติ' if clear else 'ยังสรุปไม่ได้ทางสถิติ'}"
        )

    air = air_quality(_={})["months"]
    if air:
        exceeded = sum(row["exceed_days"] or 0 for row in air)
        worst = max(air, key=lambda row: row["pm25_avg"])
        facts.append(
            f"PM2.5 เกินมาตรฐานรวม {exceeded} วัน ใน {len(air)} เดือน เดือนที่ค่าเฉลี่ยสูงสุดคือ{thai_month(worst['month'])} ที่ {worst['pm25_avg']} มคก./ลบ.ม."
        )
    facts.extend(travel_behavior(_={})["insights"])
    revenue = city_revenue(_={})["years"]
    if revenue and revenue[-1]["change_percent"] is not None:
        latest = revenue[-1]
        facts.append(
            f"รายได้ของเทศบาลเมืองพัทยาปีงบประมาณ {latest['budget_year']} รวม {latest['total_baht']:,} บาท "
            f"เปลี่ยนแปลง {latest['change_percent']:+}% เทียบปีงบประมาณก่อนหน้า"
        )
    return "\n".join(f"- {fact}" for fact in facts)


@app.get("/api/ai/summary")
def ai_summary_latest(_: dict = Depends(auth.require("view:overview"))) -> dict:
    """บทสรุปล่าสุดที่เก็บไว้ ไม่เรียก AI ใหม่ จึงเปิดหน้าเว็บกี่ครั้งก็ไม่กินโควตา"""
    latest = query_one(
        """SELECT content, model, unverified_numbers, ranking,
                  to_char(created_at AT TIME ZONE 'Asia/Bangkok', 'YYYY-MM-DD HH24:MI') AS created_at,
                  -- มีข้อมูลนำเข้าสำเร็จหลังจากสรุปครั้งนี้ บทสรุปจึงอาจไม่ตรงกับตัวเลขปัจจุบันแล้ว
                  EXISTS (SELECT 1 FROM imports i WHERE i.imported_at > s.created_at AND i.records_success > 0) AS stale
           FROM ai_summaries s ORDER BY created_at DESC, id DESC LIMIT 1"""
    )
    if latest:
        latest["unverified_numbers"] = json.loads(latest["unverified_numbers"] or "[]")
        latest["ranking"] = json.loads(latest["ranking"] or "[]")
    return {"enabled": bool(ai_summary.api_key()), "summary": latest}


@app.post("/api/ai/summary")
def ai_summary_generate(actor: dict = Depends(auth.require("manage:data"))) -> dict:
    """ให้ AI เรียบเรียงบทสรุปใหม่จากตัวเลขปัจจุบัน สั่งได้เฉพาะผู้ดูแล เพื่อคุมการใช้โควตา"""
    facts = summary_facts()
    if not facts:
        raise HTTPException(status_code=400, detail="ยังไม่มีข้อมูลให้สรุป")
    # AI เลือกและจัดลำดับได้เฉพาะคำแนะนำที่กฎของระบบสร้างไว้แล้ว ไม่ให้แต่งคำแนะนำใหม่
    options = {item["key"]: f"{item['title']} — {item['detail']}" for item in recommendations(_={})}
    try:
        result = ai_summary.generate(facts, options)
    except ai_summary.SummaryError as exc:
        audit("ai_summary_failed", actor["username"], details=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
    # ตรวจตัวเลขทั้งในบทสรุปและในเหตุผลของลำดับ เทียบกับทุกอย่างที่ส่งให้ AI
    written = " ".join([result["content"], *(item["reason"] for item in result["ranking"])])
    unverified = ai_summary.unverified_numbers(written, facts + "\n" + "\n".join(options.values()))
    query(
        """INSERT INTO ai_summaries (content, facts, model, unverified_numbers, ranking, created_by)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        [result["content"], facts, result["model"], json.dumps(unverified),
         json.dumps(result["ranking"], ensure_ascii=False), actor["username"]],
    )
    audit("ai_summary", actor["username"], result["model"], f"ตัวเลขที่ตรวจไม่พบในข้อมูล {len(unverified)} ตัว")
    return ai_summary_latest(_={})


# ---------------------------------------------------------------- รายงาน Excel


@app.get("/api/pattaya/report")
def pattaya_report(source_file: str | None = Query(default=None),
    _: dict = Depends(auth.require("view:sources")),
) -> dict:
    parameters: list[Any] = ["พัทยา ชลบุรี"]
    source_filter = "WHERE sheet_name = %s"
    if source_file:
        source_filter += " AND source_file = %s"
        parameters.append(source_file)
    metrics = query(
        f"""SELECT metric_label, metric_value, unit, year, quarter, source_row, source_file
            FROM pattaya_report_metrics {source_filter}
            ORDER BY source_row, id LIMIT 250""",
        parameters,
    )
    raw = query_one(
        "SELECT COUNT(*) AS total FROM raw_excel_cells WHERE sheet_name = %s"
        + (" AND source_file = %s" if source_file else ""),
        parameters,
    )
    return {"source_file": source_file, "raw_cells": (raw or {}).get("total", 0), "metrics": metrics}
