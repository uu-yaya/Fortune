#!/usr/bin/env python3
"""Bootstrap login accounts for password-only launch."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Allow execution via "python scripts/xxx.py" from repo root or container workdir.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("USER_AGENT", "fortune-telling-admin-script")

from server import _create_user_by_phone, _get_user_by_phone, _password_valid


PHONE_RE = re.compile(r"^1\d{10}$")


def _load_entries(args: argparse.Namespace) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for raw in args.entry:
        if ":" not in raw:
            raise ValueError(f"invalid --entry format: {raw!r}; expected phone:password")
        phone, pwd = raw.split(":", 1)
        entries.append((phone.strip(), pwd.strip()))

    if args.seed_file:
        for idx, line in enumerate(Path(args.seed_file).read_text(encoding="utf-8").splitlines(), start=1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "," in s:
                phone, pwd = s.split(",", 1)
            elif ":" in s:
                phone, pwd = s.split(":", 1)
            else:
                raise ValueError(f"invalid seed file line {idx}: {line!r}")
            entries.append((phone.strip(), pwd.strip()))
    return entries


def _validate(phone: str, password: str) -> None:
    if not PHONE_RE.fullmatch(phone):
        raise ValueError(f"invalid phone: {phone!r}")
    if not _password_valid(password):
        raise ValueError(f"invalid password for {phone!r}: must be 8-12 alnum")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create users for password-only deployment.")
    parser.add_argument(
        "--entry",
        action="append",
        default=[],
        metavar="PHONE:PASSWORD",
        help="single seed entry, repeatable",
    )
    parser.add_argument(
        "--seed-file",
        default="",
        help="utf-8 file with PHONE,PASSWORD or PHONE:PASSWORD per line",
    )
    parser.add_argument("--dry-run", action="store_true", help="validate and preview only")
    args = parser.parse_args()

    try:
        pairs = _load_entries(args)
        if not pairs:
            raise ValueError("no input entries; use --entry or --seed-file")
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print("phone\taccount\tstatus")
    exit_code = 0
    for phone, password in pairs:
        try:
            _validate(phone, password)
            existing = _get_user_by_phone(phone)
            if existing:
                print(f"{phone}\t{existing.get('account','')}\texists")
                continue
            if args.dry_run:
                print(f"{phone}\t-\twill_create")
                continue
            created = _create_user_by_phone(phone, password=password)
            print(f"{phone}\t{created.get('account','')}\tcreated")
        except Exception as exc:
            print(f"{phone}\t-\terror:{exc}", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
