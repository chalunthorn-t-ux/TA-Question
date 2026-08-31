"""จัดการบัญชีผู้ใช้จากบรรทัดคำสั่ง — ใช้แทนการแก้ storage/users.json ด้วยมือ

การแก้ไฟล์เองเสี่ยงทำ JSON พัง แล้วจะกลายเป็นว่าทุกคนล็อกอินไม่ได้
โดยที่ระบบดูเหมือนยังไม่มีใครสมัคร ซึ่งไล่หาสาเหตุยาก

ถ้าตั้ง DATABASE_URL ไว้ ทุกคำสั่งจะทำงานกับ Postgres แทนไฟล์ JSON ให้อัตโนมัติ

วิธีใช้
    python scripts/users.py list
    python scripts/users.py add somchai
    python scripts/users.py add teena --admin
    python scripts/users.py passwd somchai
    python scripts/users.py role somchai admin
    python scripts/users.py delete somchai
    python scripts/users.py init-db          # สร้างตารางใน Postgres (เรียกซ้ำได้)
    python scripts/users.py migrate          # ย้าย storage/users.json เข้า Postgres
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, db, users_repo  # noqa: E402


def _ask_password(username: str) -> str:
    for _ in range(3):
        pw = getpass.getpass(f"รหัสผ่านใหม่ของ {username}: ")
        again = getpass.getpass("พิมพ์อีกครั้งเพื่อยืนยัน: ")
        if pw != again:
            print("  ✗ รหัสผ่านไม่ตรงกัน ลองใหม่\n")
            continue
        try:
            auth._validate_password(pw)
        except auth.AuthError as exc:
            print(f"  ✗ {exc}\n")
            continue
        return pw
    print("ลองเกินจำนวนที่กำหนด ยกเลิก")
    sys.exit(1)


def cmd_list(_: argparse.Namespace) -> int:
    print(f"ที่เก็บข้อมูล: {auth.storage_backend()}\n")

    users = auth.list_users()
    if auth.users_file_error:
        print(f"✗ {auth.users_file_error}")
        return 1

    if not users:
        print("ยังไม่มีบัญชีในระบบ — คนแรกที่สมัครจะได้สิทธิ์ admin")
        return 0

    print(f"{'ชื่อผู้ใช้':<22} {'สิทธิ์':<8} {'สร้างเมื่อ':<22} เข้าล่าสุด")
    print("-" * 78)
    for u in users:
        print(f"{u['username']:<22} {u['role']:<8} {u['created_at']:<22} {u['last_login'] or '-'}")
    print(f"\nรวม {len(users)} บัญชี")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    username = auth.normalize_username(args.username)
    try:
        auth._validate_username(username)
    except auth.AuthError as exc:
        print(f"✗ {exc}")
        return 1

    if users_repo.get(username) is not None:
        print(f"✗ มีบัญชี '{username}' อยู่แล้ว (ใช้ passwd เพื่อเปลี่ยนรหัส)")
        return 1

    password = _ask_password(username)

    try:
        # register() ให้ admin กับคนแรกเท่านั้น ถ้าสั่ง --admin ต้องเลื่อนสิทธิ์ต่อเอง
        user = auth.register(username, password, password)
        if args.admin and not user.is_admin:
            auth.set_role(username, auth.ROLE_ADMIN, actor="cli")
            user = auth.User(username=username, role=auth.ROLE_ADMIN)
    except auth.AuthError as exc:
        print(f"✗ {exc}")
        return 1

    print(f"✓ สร้างบัญชี '{user.username}' สิทธิ์ {user.role} แล้ว")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    username = auth.normalize_username(args.username)
    if users_repo.get(username) is None:
        print(f"✗ ไม่พบบัญชี '{username}'")
        return 1

    password = _ask_password(username)
    try:
        auth.set_password(username, password, password)
    except auth.AuthError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ เปลี่ยนรหัสผ่านของ '{username}' แล้ว")
    return 0


def cmd_role(args: argparse.Namespace) -> int:
    username = auth.normalize_username(args.username)
    try:
        auth.set_role(username, args.role, actor="cli")
    except auth.AuthError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ เปลี่ยนสิทธิ์ '{username}' เป็น {args.role} แล้ว")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    username = auth.normalize_username(args.username)
    if users_repo.get(username) is None:
        print(f"✗ ไม่พบบัญชี '{username}'")
        return 1

    if input(f"ยืนยันลบบัญชี '{username}'? พิมพ์ yes: ").strip().lower() != "yes":
        print("ยกเลิก")
        return 1

    try:
        auth.delete_user(username, actor="cli")
    except auth.AuthError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ ลบบัญชี '{username}' แล้ว")
    return 0


def cmd_init_db(_: argparse.Namespace) -> int:
    if not db.enabled():
        print("✗ ยังไม่ได้ตั้ง DATABASE_URL ใน .env")
        print("  ผูก Postgres ที่ Vercel -> Storage -> Neon แล้วก็อป connection string มาใส่")
        return 1

    try:
        db.init_schema()
    except Exception as exc:      # noqa: BLE001
        print(f"✗ สร้างตารางไม่สำเร็จ: {exc}")
        return 1
    print("✓ ตาราง users และ unanswered พร้อมใช้งานแล้ว")
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    """ย้ายบัญชีจาก storage/users.json เข้า Postgres — hash รหัสผ่านย้ายไปตรง ๆ ไม่ต้องตั้งใหม่"""
    if not db.enabled():
        print("✗ ยังไม่ได้ตั้ง DATABASE_URL ใน .env — ไม่มีปลายทางให้ย้ายไป")
        return 1

    from_file = users_repo.load_json_file()
    if not from_file:
        print("ไม่พบบัญชีใน storage/users.json — ไม่มีอะไรต้องย้าย")
        return 0

    try:
        db.init_schema()
    except Exception as exc:      # noqa: BLE001
        print(f"✗ เตรียมตารางไม่สำเร็จ: {exc}")
        return 1

    moved = 0
    for username, record in from_file.items():
        try:
            users_repo.upsert(username, record)
        except users_repo.RepoError as exc:
            print(f"  ❌ {username:<22} {exc}")
            continue
        print(f"  ✅ {username:<22} {record.get('role', 'member')}")
        moved += 1

    print(f"\n✓ ย้าย {moved}/{len(from_file)} บัญชีเข้าฐานข้อมูลแล้ว")
    print("  ล็อกอินด้วยรหัสผ่านเดิมได้ทันที (ย้าย hash ไปตรง ๆ)")
    print("  เมื่อยืนยันว่าใช้ได้แล้ว ให้เอา storage/users.json ออกจาก repo ด้วย")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="จัดการบัญชีผู้ใช้ของ TA Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="แสดงบัญชีทั้งหมด").set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="สร้างบัญชีใหม่")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true", help="ให้สิทธิ์ผู้ดูแลระบบ")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("passwd", help="เปลี่ยนรหัสผ่าน")
    p.add_argument("username")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("role", help="เปลี่ยนสิทธิ์")
    p.add_argument("username")
    p.add_argument("role", choices=[auth.ROLE_ADMIN, auth.ROLE_MEMBER])
    p.set_defaults(func=cmd_role)

    p = sub.add_parser("delete", help="ลบบัญชี")
    p.add_argument("username")
    p.set_defaults(func=cmd_delete)

    sub.add_parser("init-db", help="สร้างตารางใน Postgres").set_defaults(func=cmd_init_db)
    sub.add_parser(
        "migrate", help="ย้าย storage/users.json เข้า Postgres"
    ).set_defaults(func=cmd_migrate)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
