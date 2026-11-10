"""Command-line admin tasks, run inside the container:

    docker compose exec -u 1000:1000 ttrpg python -m app.manage list-users
    docker compose exec -u 1000:1000 ttrpg python -m app.manage reset-password [username]

(or use ./reset-password.sh on the Docker host, which does the second one for you).
Run as the app user (PUID:PGID), not root, so the database files keep the right owner.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from . import db, security

MIN_LENGTH = 10


def list_users() -> int:
    users = security.list_users()
    if not users:
        print("No users yet. The admin from .env is created when the server starts.")
        return 0
    for u in users:
        print(f"{u['username']}{'  (admin)' if u['is_admin'] else ''}")
    return 0


def reset_password(username: str | None) -> int:
    if not username:
        list_users()
        username = input("Username: ").strip()
    user = security.get_user_by_name(username)
    if not user:
        print(f"No user named '{username}'. Existing users:", file=sys.stderr)
        list_users()
        return 1
    for _ in range(3):
        pw = getpass.getpass(f"New password for {user['username']} (at least {MIN_LENGTH} characters): ")
        if len(pw) < MIN_LENGTH:
            print(f"Too short: use at least {MIN_LENGTH} characters.")
            continue
        if getpass.getpass("Repeat it: ") != pw:
            print("The two entries don't match.")
            continue
        security.set_password(user["id"], pw)
        print(f"Password changed for {user['username']}. Sign in with it now; no restart needed.")
        print("(If sign-in is still refused, too many failed attempts locked it for a few minutes: wait, or run 'docker compose restart'.)")
        return 0
    print("Password not changed.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.manage", description="TTRPG transcription server admin tasks")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-users", help="list dashboard accounts")
    rp = sub.add_parser("reset-password", help="set a new password for a dashboard account")
    rp.add_argument("username", nargs="?")
    args = p.parse_args(argv)
    db.init()
    if args.cmd == "list-users":
        return list_users()
    return reset_password(args.username)


if __name__ == "__main__":
    sys.exit(main())
