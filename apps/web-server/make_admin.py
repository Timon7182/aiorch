#!/usr/bin/env python3
"""Promote (or create) a user as a global admin.

The admin screen is gated on the global ``role == "admin"``. Bootstrap the first
admin with this script, then manage everyone else from the UI.

Usage (from apps/web-server/):

    python make_admin.py daniyar.serikov@uco.kz

Notes:
- If the user exists, their ``role`` is set to ``admin``, ``status`` to
  ``active`` and ``is_active`` to 1.
- If the user does NOT exist yet, pass ``--create --password <pw> --name <name>``
  to create them as an active admin.
- Operates directly on the SQLite DB at ~/.magestic-ai/data.db (no server
  needed). Set MAGESTIC_DATA_DIR to override the data directory.
"""

import argparse
import os
import sqlite3
import sys
import uuid
from pathlib import Path


def _data_dir() -> Path:
    override = os.environ.get("MAGESTIC_DATA_DIR")
    return Path(override) if override else (Path.home() / ".magestic-ai")


def _hash_password(password: str) -> str:
    # passlib/bcrypt is the scheme the server uses; reuse it for compatibility.
    from passlib.context import CryptContext

    return CryptContext(schemes=["bcrypt"], deprecated="auto").hash(password)


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote a user to global admin")
    parser.add_argument("email", help="Email of the user to promote")
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create the user if they do not exist (needs --password)",
    )
    parser.add_argument("--password", help="Initial password when --create is used")
    parser.add_argument("--name", help="Display name when --create is used")
    args = parser.parse_args()

    db_path = _data_dir() / "data.db"
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path}", file=sys.stderr)
        print("Start the web server once so the DB is created.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT id, role, status FROM users WHERE email = ?", (args.email,)
        ).fetchone()

        if row is None:
            if not args.create:
                print(
                    f"ERROR: no user with email {args.email!r}. "
                    "Pass --create --password <pw> to create one.",
                    file=sys.stderr,
                )
                return 1
            if not args.password:
                print("ERROR: --create requires --password", file=sys.stderr)
                return 1
            uid = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO users (id, email, name, password_hash, role, "
                "is_active, status) VALUES (?, ?, ?, ?, 'admin', 1, 'active')",
                (
                    uid,
                    args.email,
                    args.name or args.email.split("@")[0],
                    _hash_password(args.password),
                ),
            )
            conn.commit()
            print(f"Created admin user {args.email} (id={uid})")
            return 0

        uid = row[0]
        cur.execute(
            "UPDATE users SET role='admin', status='active', is_active=1 "
            "WHERE id = ?",
            (uid,),
        )
        conn.commit()
        print(f"Promoted {args.email} to admin (id={uid})")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
