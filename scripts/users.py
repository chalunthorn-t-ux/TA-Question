"""จัดการบัญชีผู้ใช้จากบรรทัดคำสั่ง — ใช้แทนการแก้ storage/users.json ด้วยมือ

การแก้ไฟล์เองเสี่ยงทำ JSON พัง แล้วจะกลายเป็นว่าทุกคนล็อกอินไม่ได้
โดยที่ระบบดูเหมือนยังไม่มีใครสมัคร ซึ่งไล่หาสาเหตุยาก

วิธีใช้
    python scripts/users.py list
    python scripts/users.py add somchai
    python scripts/users.py add teena --admin
    python scripts/users.py passwd somchai
    python scripts/users.py role somchai admin
    python scripts/users.py delete somchai
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth  # noqa: E402


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
    if auth.users_file_error:
        print(f"✗ {auth.users_file_error}")
        return 1

    users = auth.list_users()
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

    users = auth.load_users()
    if username in users:
        print(f"✗ มีบัญชี '{username}' อยู่แล้ว (ใช้ passwd เพื่อเปลี่ยนรหัส)")
        return 1

    password = _ask_password(username)

    # register() ให้ admin กับคนแรกเท่านั้น ถ้าสั่ง --admin ต้องตั้งเอง
    user = auth.register(username, password, password)
    if args.admin and not user.is_admin:
        users = auth.load_users()
        users[username]["role"] = auth.ROLE_ADMIN
        auth._save_users(users)
        user = auth.User(username=username, role=auth.ROLE_ADMIN)

    print(f"✓ สร้างบัญชี '{user.username}' สิทธิ์ {user.role} แล้ว")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    username = auth.normalize_username(args.username)
    users = auth.load_users()
    if username not in users:
        print(f"✗ ไม่พบบัญชี '{username}'")
        return 1

    password = _ask_password(username)
    users[username]["password"] = auth.hash_password(password)
    auth._save_users(users)
    print(f"✓ เปลี่ยนรหัสผ่านของ '{username}' แล้ว")
    return 0


def cmd_role(args: argparse.Namespace) -> int:
    username = auth.normalize_username(args.username)
    users = auth.load_users()
    if username not in users:
        print(f"✗ ไม่พบบัญชี '{username}'")
        return 1

    if args.role == auth.ROLE_MEMBER:
        admins = [n for n, r in users.items() if r.get("role") == auth.ROLE_ADMIN]
        if admins == [username]:
            print("✗ ลดสิทธิ์ไม่ได้ เพราะจะไม่มี admin เหลือในระบบ")
            return 1

    users[username]["role"] = args.role
    auth._save_users(users)
    print(f"✓ เปลี่ยนสิทธิ์ '{username}' เป็น {args.role} แล้ว")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    username = auth.normalize_username(args.username)
    users = auth.load_users()
    if username not in users:
        print(f"✗ ไม่พบบัญชี '{username}'")
        return 1

    admins = [n for n, r in users.items() if r.get("role") == auth.ROLE_ADMIN]
    if admins == [username]:
        print("✗ ลบไม่ได้ เพราะเป็น admin คนเดียวที่เหลือ")
        return 1

    if input(f"ยืนยันลบบัญชี '{username}'? พิมพ์ yes: ").strip().lower() != "yes":
        print("ยกเลิก")
        return 1

    del users[username]
    auth._save_users(users)
    print(f"✓ ลบบัญชี '{username}' แล้ว")
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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
