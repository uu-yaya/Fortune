#!/usr/bin/env python3
"""Admin password reset helper for password-only operation."""

from __future__ import annotations

import argparse
import os
import re
import sys

# Allow execution via "python scripts/xxx.py" from repo root or container workdir.
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("USER_AGENT", "fortune-telling-admin-script")

from server import (
    _get_user_by_account,
    _get_user_by_phone,
    _password_valid,
    _update_user_password,
)


PHONE_RE = re.compile(r"^1\d{10}$")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset one user password by account or phone.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--account", default="", help="account like JIYI-AB12CD34")
    group.add_argument("--phone", default="", help="11-digit mainland phone")
    parser.add_argument("--new-password", required=True, help="new password (8-12 alnum)")
    parser.add_argument("--dry-run", action="store_true", help="lookup only, no update")
    args = parser.parse_args()

    new_password = str(args.new_password or "").strip()
    if not _password_valid(new_password):
        print("[ERROR] invalid --new-password: must be 8-12 letters/digits", file=sys.stderr)
        return 2

    if args.phone and not PHONE_RE.fullmatch(str(args.phone).strip()):
        print("[ERROR] invalid --phone: must be 11-digit mainland phone", file=sys.stderr)
        return 2

    user = None
    if args.account:
        user = _get_user_by_account(str(args.account).strip())
    elif args.phone:
        user = _get_user_by_phone(str(args.phone).strip())

    if not user:
        print("[ERROR] user not found", file=sys.stderr)
        return 3

    if args.dry_run:
        print(
            "dry-run matched "
            f"account={user.get('account','')} phone={user.get('phone','')} id={user.get('id','')}"
        )
        return 0

    _update_user_password(int(user["id"]), new_password)
    print(f"updated account={user.get('account','')} phone={user.get('phone','')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
