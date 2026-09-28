"""Operator CLI (run inside the api/engine container).

    docker compose exec api python -m app.cli create-admin
    echo "$PW" | docker compose exec -T api python -m app.cli create-admin alice --password-stdin
    docker compose exec api python -m app.cli reset-password <username>
    docker compose exec api python -m app.cli disable-totp <username>
    docker compose exec api python -m app.cli kill            # emergency stop from a shell
    docker compose exec api python -m app.cli status
    python -m app.cli gen-vapid                               # prints a VAPID key pair for .env
    python -m app.cli gen-secret                              # prints a random AUTH_SECRET
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import secrets
import sys

from sqlalchemy import func, select, update

from app.api.security import hash_password, password_problems
from app.config import get_settings
from app.core.store import Store
from app.db import models as M
from app.db.session import init_engine, session_factory


def _store() -> Store:
    init_engine(get_settings().database_url)
    return Store(session_factory())


def _ask_password(username: str) -> str:
    while True:
        pw = getpass.getpass("New password: ")
        problems = password_problems(pw, username)
        if problems:
            print("Password rejected: " + ", ".join(problems))
            continue
        if getpass.getpass("Repeat password: ") != pw:
            print("Passwords do not match.")
            continue
        return pw


async def create_admin(username: str | None, password_stdin: bool = False) -> int:
    store = _store()
    username = (username or input("Username: ")).strip()
    if not username or len(username) > 64:
        print("invalid username")
        return 1
    async with store.factory() as s:
        exists = (await s.execute(select(func.count()).select_from(M.User).where(M.User.username == username))).scalar()
        if exists:
            print(f"user {username!r} already exists (use reset-password)")
            return 1
    if password_stdin:
        pw = sys.stdin.readline().rstrip("\r\n")
        problems = password_problems(pw, username)
        if problems:
            print("Password rejected: " + ", ".join(problems))
            return 1
    else:
        pw = _ask_password(username)
    async with store.factory() as s:
        s.add(M.User(username=username, password_hash=hash_password(pw)))
        await s.commit()
    await store.audit("user_created", username=username, data={"via": "cli"})
    print(f"Created user {username!r}. Log in, then enable two-factor authentication under Security.")
    return 0


async def reset_password(username: str) -> int:
    store = _store()
    pw = _ask_password(username)
    async with store.factory() as s:
        r = await s.execute(update(M.User).where(M.User.username == username)
                            .values(password_hash=hash_password(pw), failed_logins=0, locked_until=None))
        await s.execute(update(M.Session).where(M.Session.user_id.in_(
            select(M.User.id).where(M.User.username == username))).values(revoked=True))
        await s.commit()
    if r.rowcount == 0:
        print("no such user")
        return 1
    await store.audit("password_reset", username=username, data={"via": "cli"})
    print("Password reset; all sessions revoked.")
    return 0


async def disable_totp(username: str) -> int:
    store = _store()
    async with store.factory() as s:
        r = await s.execute(update(M.User).where(M.User.username == username)
                            .values(totp_enabled=False, totp_secret_enc=None, recovery_codes=[]))
        await s.commit()
    if r.rowcount == 0:
        print("no such user")
        return 1
    await store.update_trading({"mode": "paper", "strategy_enabled": False}, "cli")
    await store.audit("totp_disabled", username=username, data={"via": "cli"})
    print("2FA disabled (trading mode forced to PAPER, strategy disarmed).")
    return 0


async def kill() -> int:
    store = _store()
    await store.update_trading({"kill_switch": True, "strategy_enabled": False}, "cli")
    cid = await store.enqueue_command("kill", {"reason": "CLI kill"}, "cli")
    await store.audit("kill_switch", username="cli", data={"via": "cli"})
    print("Kill switch engaged in the database; waiting for the engine to flatten/cancel...")
    for _ in range(80):
        c = await store.get_command(cid)
        if c and c.status != "pending":
            print(f"engine: {c.status} {c.result}")
            return 0
        await asyncio.sleep(0.25)
    print("Engine did not respond within 20s. New trades are blocked; check exchange positions manually.")
    return 2


async def status() -> int:
    store = _store()
    st = await store.trading_state()
    comps = await store.components()
    print(f"mode={st['mode']} kill_switch={st['kill_switch']} halted={st['halted']} ({st['halt_reason']}) "
          f"strategy_enabled={st['strategy_enabled']}")
    for k, v in sorted(comps.items()):
        print(f"  {k:14} {v.status:9} {v.updated_at:%Y-%m-%d %H:%M:%S}  {v.detail or ''}")
    return 0


def gen_vapid() -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    print(f"VAPID_PUBLIC_KEY={b64(pub)}")
    print(f"VAPID_PRIVATE_KEY={b64(priv)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="kestrel")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin")
    a.add_argument("username", nargs="?")
    a.add_argument("--password-stdin", action="store_true", help="read the password from stdin (automation)")
    b = sub.add_parser("reset-password")
    b.add_argument("username")
    d = sub.add_parser("disable-totp")
    d.add_argument("username")
    sub.add_parser("kill")
    sub.add_parser("status")
    sub.add_parser("gen-vapid")
    sub.add_parser("gen-secret")
    args = ap.parse_args()
    if args.cmd == "gen-vapid":
        return gen_vapid()
    if args.cmd == "gen-secret":
        print(secrets.token_urlsafe(48))
        return 0
    fn = {"create-admin": lambda: create_admin(args.username, args.password_stdin), "reset-password": lambda: reset_password(args.username),
          "disable-totp": lambda: disable_totp(args.username), "kill": kill, "status": status}[args.cmd]
    return asyncio.run(fn())


if __name__ == "__main__":
    sys.exit(main())
