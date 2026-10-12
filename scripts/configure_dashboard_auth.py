"""Manage the local Dashboard break-glass administrator account."""

from __future__ import annotations

import argparse
import getpass
import secrets
import sqlite3
import string
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH
from src.auth.emergency import initialize_break_glass, rotate_password, set_enabled
from src.auth.repository import bootstrap_primary_admin
from src.db.migrations import run_migrations
from src.db.schema import apply_schema

PASSWORD_ALPHABET = string.ascii_letters + string.digits + "-_!@#$%"


def generate_password(length: int = 24) -> str:
    """Return a cryptographically random break-glass password."""
    if length < 16:
        raise ValueError("Generated password length must be at least 16 characters")
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def _open_database(database: str | Path) -> sqlite3.Connection:
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        apply_schema(conn)
        run_migrations(conn)
        bootstrap_primary_admin(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


def _password_from_args(args: argparse.Namespace, *, confirmation_label: str) -> str:
    supplied = getattr(args, "password", None)
    if supplied:
        return supplied
    if getattr(args, "generate_password", False):
        return generate_password(getattr(args, "password_length", 24))
    first = getpass.getpass(f"{confirmation_label} password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise ValueError("Passwords do not match")
    return first


def run_command(args: argparse.Namespace) -> None:
    conn = _open_database(args.database)
    try:
        if args.command == "init":
            password = _password_from_args(args, confirmation_label="Break-glass")
            result = initialize_break_glass(
                conn,
                username=args.username,
                password=password,
                display_name=args.display_name,
            )
            conn.commit()
            print("Break-glass administrator initialized.")
            print(f"User ID: {result['user_id']}")
            print(f"Username: {result['username']}")
            if args.generate_password:
                print(f"Generated password (shown once): {password}")
            return

        if args.command == "rotate-password":
            password = _password_from_args(args, confirmation_label="New break-glass")
            rotate_password(conn, username=args.username, password=password)
            conn.commit()
            print(f"Password rotated for {args.username}; active sessions were revoked.")
            if args.generate_password:
                print(f"Generated password (shown once): {password}")
            return

        if args.command in {"enable", "disable"}:
            enabled = args.command == "enable"
            set_enabled(conn, username=args.username, enabled=enabled)
            conn.commit()
            state = "enabled" if enabled else "disabled"
            print(f"Break-glass administrator {args.username} {state}.")
            return

        raise ValueError(f"Unsupported command: {args.command}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_password_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--password",
        help="Supply a password non-interactively. Avoid shell history on shared systems.",
    )
    group.add_argument(
        "--generate-password",
        action="store_true",
        help="Generate a password and print it once after the operation succeeds.",
    )
    parser.add_argument(
        "--password-length",
        type=int,
        default=24,
        help="Length used with --generate-password (minimum: 16).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=str(DB_PATH),
        help="Dashboard SQLite database path (defaults to config.database_file).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize the break-glass Admin.")
    init_parser.add_argument("--username", default="break-glass-admin")
    init_parser.add_argument("--display-name", default="Break-glass Admin")
    _add_password_options(init_parser)

    password_parser = subparsers.add_parser(
        "rotate-password", help="Rotate the password and revoke active sessions."
    )
    password_parser.add_argument("username")
    _add_password_options(password_parser)

    for command, help_text in (
        ("enable", "Enable the break-glass administrator."),
        ("disable", "Disable the break-glass administrator and revoke sessions."),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("username")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run_command(args)
    except KeyError as exc:
        parser.error(f"Break-glass account not found: {exc.args[0]}")
    except (RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
