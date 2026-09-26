"""ยืนยันตัวตนและการจำกัดสิทธิ์ตามบทบาท (Role-Based Access Control)

เข้ารหัสรหัสผ่านด้วย hashlib.scrypt ของไลบรารีมาตรฐาน ไม่เพิ่มไลบรารีภายนอก
เหตุผลเดียวกับ analytics.py คือให้ตรวจสอบได้ทุกบรรทัดและติดตั้งง่าย
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request, Response

# ---------------------------------------------------------------- สิทธิ์และบทบาท

# สิทธิ์ทั้งหมดในระบบ ใช้คุมทีละกลุ่มข้อมูล ไม่ใช่ทีละ endpoint เพื่อให้เพิ่ม endpoint ใหม่แล้วไม่ต้องแก้ตารางสิทธิ์
PERMISSIONS = (
    "view:overview",         # KPI ภาพรวม นักท่องเที่ยว อากาศ วันหยุด
    "view:spatial",          # แผนที่ แยกจราจร ย่านท่องเที่ยว สถานที่
    "view:forecast",         # พยากรณ์และผลกระทบของฝน/วันหยุด
    "view:recommendations",  # ข้อเสนอแนะเชิงกลยุทธ์
    "view:demographics",     # ประชากรแฝง
    "view:sources",          # แหล่งข้อมูลและประวัติการนำเข้า
    "export:report",         # ออกรายงานผู้บริหาร
    "manage:users",          # จัดการผู้ใช้และสิทธิ์
)

# ผู้ประกอบการเห็นข้อมูลที่ใช้วางแผนธุรกิจของตัวเองได้ แต่ไม่เห็นประชากรแฝงซึ่งเป็นข้อมูลระดับนโยบายเมือง
# และไม่เห็นประวัติการนำเข้าซึ่งเป็นเรื่องการดูแลระบบ
ROLE_DEFINITIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "admin": ("ผู้ดูแลระบบ", PERMISSIONS),
    "executive": ("ผู้บริหาร", tuple(p for p in PERMISSIONS if p != "manage:users")),
    "operator": ("ผู้ประกอบการ", (
        "view:overview", "view:spatial", "view:forecast", "view:recommendations", "export:report",
    )),
}

SESSION_COOKIE = "pattaya_session"
SESSION_HOURS = 12

# สถานะบัญชี คนที่สมัครเองเริ่มที่ pending เข้าสู่ระบบไม่ได้จนกว่าผู้ดูแลจะอนุมัติ
STATUS_PENDING, STATUS_ACTIVE, STATUS_REJECTED = "pending", "active", "rejected"

# บทบาทที่เปิดให้เลือกตอนสมัครเอง ไม่ให้เลือก admin เพราะไม่มีใครควรตั้งตัวเองเป็นผู้ดูแลระบบได้
SELF_SIGNUP_ROLES = ("executive", "operator")

# จำกัดจำนวนครั้งที่สมัครได้ต่อ IP กันการยิงสร้างบัญชีรัว ๆ เก็บในหน่วยความจำของโปรเซส
# พอสำหรับต้นแบบที่รันเครื่องเดียว ถ้าขึ้นใช้งานจริงหลายเครื่องต้องย้ายไปเก็บที่ส่วนกลาง
SIGNUP_LIMIT, SIGNUP_WINDOW_SECONDS = 5, 3600
_signup_attempts: dict[str, list[float]] = {}


def signup_allowed(client: str) -> bool:
    import time
    now = time.time()
    recent = [t for t in _signup_attempts.get(client, []) if now - t < SIGNUP_WINDOW_SECONDS]
    _signup_attempts[client] = recent
    if len(recent) >= SIGNUP_LIMIT:
        return False
    recent.append(now)
    return True

# ---------------------------------------------------------------- รหัสผ่าน

# พารามิเตอร์ scrypt ตามคำแนะนำทั่วไป n=2^14 ใช้หน่วยความจำราว 16 MB ต่อการตรวจหนึ่งครั้ง
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_MAXMEM = 2 ** 14, 8, 1, 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=SCRYPT_MAXMEM
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    """ตรวจรหัสผ่าน คืน False ถ้ารูปแบบที่เก็บไว้เสียหาย แทนที่จะโยน exception"""
    if not stored:
        return False
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(key_hex)
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    # เทียบแบบเวลาคงที่ กันการเดาทีละไบต์จากเวลาที่ใช้ตอบ
    return hmac.compare_digest(actual, expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- ติดตั้งข้อมูลตั้งต้น


def seed_roles(query, query_one) -> None:
    """สร้างบทบาทและสิทธิ์ให้ตรงกับ ROLE_DEFINITIONS ทำซ้ำได้ไม่ทำให้ข้อมูลซ้ำ"""
    for name, (description, permissions) in ROLE_DEFINITIONS.items():
        query(
            """INSERT INTO roles (name, description) VALUES (%s, %s)
               ON CONFLICT (name) DO UPDATE SET description = excluded.description""",
            [name, description],
        )
        role = query_one("SELECT id FROM roles WHERE name = %s", [name])
        for permission in permissions:
            query(
                """INSERT INTO role_permissions (role_id, permission) VALUES (%s, %s)
                   ON CONFLICT DO NOTHING""",
                [role["id"], permission],
            )
        # สิทธิ์ที่ถอดออกจากนิยามแล้ว ต้องลบออกจากฐานด้วย ไม่งั้นบทบาทจะค้างสิทธิ์เก่าไว้
        query(
            "DELETE FROM role_permissions WHERE role_id = %s AND permission <> ALL(%s)",
            [role["id"], list(permissions)],
        )


def seed_admin(query, query_one) -> Optional[str]:
    """สร้างผู้ดูแลระบบคนแรกถ้ายังไม่มีผู้ใช้เลย คืนรหัสผ่านที่สุ่มให้ถ้าไม่ได้ตั้งผ่าน .env"""
    if query_one("SELECT id FROM users LIMIT 1"):
        return None
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(12)
    user = query_one(
        """INSERT INTO users (username, full_name, password_hash) VALUES (%s, %s, %s) RETURNING id""",
        [username, "ผู้ดูแลระบบ", hash_password(password)],
    )
    role = query_one("SELECT id FROM roles WHERE name = 'admin'")
    query("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)", [user["id"], role["id"]])
    query(
        "INSERT INTO audit_logs (action, actor, target, details) VALUES ('seed_admin', 'system', %s, %s)",
        [username, "สร้างผู้ดูแลระบบคนแรกอัตโนมัติ"],
    )
    return password if generated else None


# ---------------------------------------------------------------- session


def create_session(query, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
    query(
        "INSERT INTO user_sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
        [hash_token(token), user_id, expires],
    )
    return token


def delete_session(query, token: Optional[str]) -> None:
    if token:
        query("DELETE FROM user_sessions WHERE token_hash = %s", [hash_token(token)])


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,   # JavaScript อ่านไม่ได้ ลดผลของ XSS
        samesite="lax",  # ไม่ถูกแนบไปกับคำขอข้ามเว็บแบบ POST ลดผลของ CSRF
        # ตั้ง COOKIE_SECURE=1 ใน .env เมื่อขึ้นใช้งานจริงผ่าน HTTPS ตอนพัฒนาบน http ต้องเป็น False
        secure=os.environ.get("COOKIE_SECURE", "0") == "1",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# ---------------------------------------------------------------- dependency สำหรับ FastAPI

# ผูกฟังก์ชัน query ของ main.py เข้ามาตอนเริ่มแอป เพื่อไม่ให้ auth.py ต้อง import main (จะวนกัน)
_query: Callable = None  # type: ignore[assignment]
_query_one: Callable = None  # type: ignore[assignment]


def bind(query: Callable, query_one: Callable) -> None:
    global _query, _query_one
    _query, _query_one = query, query_one


def load_user(request: Request) -> Optional[dict]:
    """อ่าน session จากคุกกี้ คืนผู้ใช้พร้อมบทบาทและสิทธิ์ คืน None ถ้ายังไม่ได้เข้าสู่ระบบ"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = _query_one(
        """SELECT u.id, u.username, u.full_name, s.expires_at
           FROM user_sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = %s AND s.expires_at > now() AND u.status = 'active'""",
        [hash_token(token)],
    )
    if not row:
        return None
    _query("UPDATE user_sessions SET last_seen_at = now() WHERE token_hash = %s", [hash_token(token)])
    roles = _query(
        """SELECT r.name, r.description FROM user_roles ur JOIN roles r ON r.id = ur.role_id
           WHERE ur.user_id = %s ORDER BY r.name""",
        [row["id"]],
    )
    permissions = _query(
        """SELECT DISTINCT rp.permission FROM user_roles ur
           JOIN role_permissions rp ON rp.role_id = ur.role_id
           WHERE ur.user_id = %s ORDER BY rp.permission""",
        [row["id"]],
    )
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "roles": [role["name"] for role in roles],
        "role_labels": [role["description"] for role in roles],
        "permissions": [item["permission"] for item in permissions],
    }


def current_user(request: Request) -> dict:
    user = load_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="กรุณาเข้าสู่ระบบ")
    return user


def require(permission: str) -> Callable:
    """dependency ที่ปล่อยผ่านเฉพาะผู้ใช้ที่มีสิทธิ์นี้

    401 = ยังไม่ได้เข้าสู่ระบบ (หน้าเว็บจะเด้งหน้าล็อกอิน)
    403 = เข้าสู่ระบบแล้วแต่บทบาทไม่มีสิทธิ์ (หน้าเว็บจะซ่อนส่วนนั้นไป)
    """
    def dependency(user: dict = Depends(current_user)) -> dict:
        if permission not in user["permissions"]:
            raise HTTPException(status_code=403, detail=f"บทบาทของคุณไม่มีสิทธิ์ {permission}")
        return user
    return dependency
