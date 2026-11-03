"""สร้าง/ลบบัญชีทดสอบสำหรับซ้อมและ demo ตอนนำเสนอ

    python scripts/demo_accounts.py create   # สร้างบัญชี แล้วเขียนรหัสผ่านลง demo_accounts.local.md
    python scripts/demo_accounts.py delete   # ลบบัญชีทดสอบทั้งหมด (ข้อมูลอื่นไม่ถูกแตะ)

บัญชีทั้งหมดขึ้นต้นด้วย demo_ แยกออกจากผู้ใช้จริงได้ชัด ไม่ได้เพิ่มข้อมูลท่องเที่ยวปลอมใด ๆ
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import auth
from app.database import connect, initialize_database

CREDS_FILE = PROJECT_ROOT / "demo_accounts.local.md"

# username, บทบาท, สถานะ, หน่วยงาน, ใช้ทำอะไร
ACCOUNTS = [
    ("demo_admin", "admin", "active", None, "อนุมัติผู้สมัคร / ดู audit log"),
    ("demo_executive", "executive", "active", None, "demo แดชบอร์ดเต็ม + ออกรายงาน PDF"),
    ("demo_operator", "operator", "active", None, "โชว์ว่าประชากรแฝง/แหล่งข้อมูลหายไป และ API ตอบ 403"),
    ("demo_pending_shop", "operator", "pending", "ร้านอาหารทดสอบ (demo)", "กดอนุมัติบนเวที"),
    ("demo_pending_hotel", "executive", "pending", "โรงแรมทดสอบ (demo)", "ลองล็อกอินก่อนอนุมัติ → 403"),
]


def create() -> None:
    initialize_database()
    rows = []
    with connect() as c:
        auth.seed_roles(
            lambda sql, params=(): c.execute(sql, tuple(params or ())),
            lambda sql, params=(): c.execute(sql, tuple(params or ())).fetchone(),
        )
        for username, role, status, org, purpose in ACCOUNTS:
            if c.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone():
                print(f"ข้าม {username} (มีอยู่แล้ว)")
                continue
            password = secrets.token_urlsafe(9)
            user_id = c.execute(
                """INSERT INTO users (username, full_name, password_hash, status, requested_role, organization)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (username, "บัญชีทดสอบ (demo)", auth.hash_password(password), status, role, org),
            ).fetchone()["id"]
            if status == "active":
                role_id = c.execute("SELECT id FROM roles WHERE name = %s", (role,)).fetchone()["id"]
                c.execute("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)", (user_id, role_id))
            c.execute(
                "INSERT INTO audit_logs (action, actor, target, details) VALUES ('create_user', 'demo_script', %s, %s)",
                (username, f"บัญชีทดสอบ บทบาท {role} สถานะ {status}"),
            )
            rows.append(f"| `{username}` | `{password}` | {role} | {status} | {purpose} |")
            print(f"สร้าง {username} ({role}, {status})")
    if rows:
        with CREDS_FILE.open("a", encoding="utf-8") as f:
            if f.tell() == 0:
                f.write("# บัญชีทดสอบสำหรับ demo (ห้าม commit)\n\n"
                        "| ชื่อผู้ใช้ | รหัสผ่าน | บทบาท | สถานะ | ใช้ทำอะไร |\n|---|---|---|---|---|\n")
            f.write("\n".join(rows) + "\n")
        print(f"รหัสผ่านอยู่ที่ {CREDS_FILE.name}")


def delete() -> None:
    with connect() as c:
        for username, *_ in ACCOUNTS:
            row = c.execute("SELECT id FROM users WHERE username = %s", (username,)).fetchone()
            if not row:
                continue
            c.execute("DELETE FROM user_sessions WHERE user_id = %s", (row["id"],))
            c.execute("DELETE FROM user_roles WHERE user_id = %s", (row["id"],))
            c.execute("DELETE FROM users WHERE id = %s", (row["id"],))
            c.execute(
                "INSERT INTO audit_logs (action, actor, target) VALUES ('delete_user', 'demo_script', %s)", (username,)
            )
            print(f"ลบ {username}")
    CREDS_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("create", "delete"):
        raise SystemExit(__doc__)
    {"create": create, "delete": delete}[sys.argv[1]]()
