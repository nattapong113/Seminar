"""จัดการผู้ใช้และบทบาทจาก command line

ใช้ตอนตั้งค่าครั้งแรก หรือตอนลืมรหัสผ่านผู้ดูแลระบบจนเข้าหน้าเว็บไม่ได้

    python scripts/manage_users.py list
    python scripts/manage_users.py create somchai executive --full-name "สมชาย ใจดี"
    python scripts/manage_users.py reset admin
    python scripts/manage_users.py set-role somchai operator
    python scripts/manage_users.py delete somchai
"""

from __future__ import annotations

import argparse
import secrets
import sys
from getpass import getpass
from pathlib import Path

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import auth
from app.database import connect, initialize_database


def ask_password(username: str) -> tuple[str, bool]:
    """ถามรหัสผ่าน ถ้าเว้นว่างจะสุ่มให้ คืน (รหัสผ่าน, สุ่มให้หรือไม่)"""
    first = getpass(f"รหัสผ่านใหม่ของ {username} (เว้นว่าง = สุ่มให้): ")
    if not first:
        return secrets.token_urlsafe(12), True
    if len(first) < 8:
        raise SystemExit("รหัสผ่านต้องยาวอย่างน้อย 8 ตัวอักษร")
    if first != getpass("พิมพ์อีกครั้งเพื่อยืนยัน: "):
        raise SystemExit("รหัสผ่านสองครั้งไม่ตรงกัน")
    return first, False


def role_id(connection: psycopg.Connection, name: str) -> int:
    row = connection.execute("SELECT id FROM roles WHERE name = %s", (name,)).fetchone()
    if not row:
        raise SystemExit(f"ไม่พบบทบาท {name} (มีให้เลือก: {', '.join(auth.ROLE_DEFINITIONS)})")
    return row["id"]


def user_id(connection: psycopg.Connection, username: str) -> int:
    row = connection.execute("SELECT id FROM users WHERE username = %s", (username,)).fetchone()
    if not row:
        raise SystemExit(f"ไม่พบผู้ใช้ {username}")
    return row["id"]


def cmd_list(connection: psycopg.Connection, _args) -> None:
    rows = connection.execute(
        """SELECT u.username, u.full_name,
                  COALESCE(string_agg(r.name, ', ' ORDER BY r.name), '-') AS roles,
                  to_char(u.created_at AT TIME ZONE 'Asia/Bangkok', 'YYYY-MM-DD HH24:MI') AS created_at
           FROM users u
           LEFT JOIN user_roles ur ON ur.user_id = u.id
           LEFT JOIN roles r ON r.id = ur.role_id
           GROUP BY u.id ORDER BY u.username"""
    ).fetchall()
    if not rows:
        print("ยังไม่มีผู้ใช้ในระบบ")
        return
    print(f"{'ชื่อผู้ใช้':<20}{'บทบาท':<26}{'ชื่อ-นามสกุล':<28}สร้างเมื่อ")
    for row in rows:
        print(f"{row['username']:<20}{row['roles']:<26}{row['full_name'] or '-':<28}{row['created_at']}")


def cmd_create(connection: psycopg.Connection, args) -> None:
    if connection.execute("SELECT id FROM users WHERE username = %s", (args.username,)).fetchone():
        raise SystemExit(f"มีผู้ใช้ {args.username} อยู่แล้ว ถ้าต้องการเปลี่ยนรหัสผ่านให้ใช้คำสั่ง reset")
    password, generated = ask_password(args.username)
    new_user = connection.execute(
        "INSERT INTO users (username, full_name, email, password_hash) VALUES (%s, %s, %s, %s) RETURNING id",
        (args.username, args.full_name, args.email, auth.hash_password(password)),
    ).fetchone()
    connection.execute(
        "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
        (new_user["id"], role_id(connection, args.role)),
    )
    connection.execute(
        "INSERT INTO audit_logs (action, actor, target, details) VALUES ('create_user', 'cli', %s, %s)",
        (args.username, f"บทบาท {args.role}"),
    )
    print(f"สร้างผู้ใช้ {args.username} บทบาท {args.role} แล้ว")
    if generated:
        print(f"รหัสผ่านที่สุ่มให้: {password}")


def cmd_reset(connection: psycopg.Connection, args) -> None:
    target = user_id(connection, args.username)
    password, generated = ask_password(args.username)
    connection.execute("UPDATE users SET password_hash = %s WHERE id = %s", (auth.hash_password(password), target))
    # บังคับให้ทุกอุปกรณ์ที่ค้างอยู่ต้องเข้าสู่ระบบใหม่ ไม่งั้นคนที่ถือคุกกี้เดิมยังใช้ต่อได้
    removed = connection.execute("DELETE FROM user_sessions WHERE user_id = %s", (target,)).rowcount
    connection.execute(
        "INSERT INTO audit_logs (action, actor, target, details) VALUES ('reset_password', 'cli', %s, %s)",
        (args.username, f"ยกเลิก session ค้าง {removed} รายการ"),
    )
    print(f"เปลี่ยนรหัสผ่านของ {args.username} แล้ว (ยกเลิก session ค้าง {removed} รายการ)")
    if generated:
        print(f"รหัสผ่านที่สุ่มให้: {password}")


def cmd_set_role(connection: psycopg.Connection, args) -> None:
    target = user_id(connection, args.username)
    connection.execute("DELETE FROM user_roles WHERE user_id = %s", (target,))
    connection.execute(
        "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)", (target, role_id(connection, args.role))
    )
    connection.execute(
        "INSERT INTO audit_logs (action, actor, target, details) VALUES ('set_role', 'cli', %s, %s)",
        (args.username, args.role),
    )
    print(f"ตั้งบทบาทของ {args.username} เป็น {args.role} แล้ว")


def cmd_delete(connection: psycopg.Connection, args) -> None:
    target = user_id(connection, args.username)
    remaining = connection.execute(
        """SELECT COUNT(*) AS total FROM user_roles ur JOIN roles r ON r.id = ur.role_id
           WHERE r.name = 'admin' AND ur.user_id <> %s""",
        (target,),
    ).fetchone()["total"]
    if not remaining:
        raise SystemExit("ลบไม่ได้ ต้องเหลือผู้ดูแลระบบอย่างน้อยหนึ่งคน")
    connection.execute("DELETE FROM user_sessions WHERE user_id = %s", (target,))
    connection.execute("DELETE FROM user_roles WHERE user_id = %s", (target,))
    connection.execute("DELETE FROM users WHERE id = %s", (target,))
    connection.execute(
        "INSERT INTO audit_logs (action, actor, target) VALUES ('delete_user', 'cli', %s)", (args.username,)
    )
    print(f"ลบผู้ใช้ {args.username} แล้ว")


def main() -> None:
    parser = argparse.ArgumentParser(description="จัดการผู้ใช้และบทบาทของระบบ")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="แสดงผู้ใช้ทั้งหมด").set_defaults(run=cmd_list)

    create = sub.add_parser("create", help="สร้างผู้ใช้ใหม่")
    create.add_argument("username")
    create.add_argument("role", choices=sorted(auth.ROLE_DEFINITIONS))
    create.add_argument("--full-name", default=None)
    create.add_argument("--email", default=None)
    create.set_defaults(run=cmd_create)

    reset = sub.add_parser("reset", help="เปลี่ยนรหัสผ่าน")
    reset.add_argument("username")
    reset.set_defaults(run=cmd_reset)

    set_role = sub.add_parser("set-role", help="เปลี่ยนบทบาท")
    set_role.add_argument("username")
    set_role.add_argument("role", choices=sorted(auth.ROLE_DEFINITIONS))
    set_role.set_defaults(run=cmd_set_role)

    delete = sub.add_parser("delete", help="ลบผู้ใช้")
    delete.add_argument("username")
    delete.set_defaults(run=cmd_delete)

    args = parser.parse_args()
    initialize_database()
    with connect() as connection:
        # บทบาทและสิทธิ์ต้องมีก่อนเสมอ เผื่อรันสคริปต์นี้ก่อนเปิดเซิร์ฟเวอร์ครั้งแรก
        auth.seed_roles(
            lambda sql, params=(): connection.execute(sql, tuple(params or ())),
            lambda sql, params=(): connection.execute(sql, tuple(params or ())).fetchone(),
        )
        args.run(connection, args)


if __name__ == "__main__":
    main()
