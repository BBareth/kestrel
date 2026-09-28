"""Authentication primitives: argon2id hashing, session tokens, TOTP, rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
# Verified against when the username does not exist, so timing does not reveal valid usernames.
_DUMMY_HASH = _ph.hash("kestrel-dummy-password-for-timing")

MIN_PASSWORD_LENGTH = 12


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(stored: str | None, pw: str) -> bool:
    try:
        return _ph.verify(stored or _DUMMY_HASH, pw) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return _ph.check_needs_rehash(stored)
    except InvalidHashError:
        return True


def password_problems(pw: str, username: str = "") -> list[str]:
    out = []
    if len(pw) < MIN_PASSWORD_LENGTH:
        out.append(f"at least {MIN_PASSWORD_LENGTH} characters")
    if username and username.lower() in pw.lower():
        out.append("must not contain the username")
    if len(set(pw)) < 6:
        out.append("too repetitive")
    return out


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def consteq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _fernet(secret: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"kestrel-totp-v1", info=b"totp-secret").derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(auth_secret: str, value: str) -> str:
    return _fernet(auth_secret).encrypt(value.encode()).decode()


def decrypt_secret(auth_secret: str, token: str) -> str | None:
    try:
        return _fernet(auth_secret).decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def new_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Kestrel")


def new_recovery_codes(n: int = 8) -> list[str]:
    return [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(n)]


class RateLimiter:
    """In-memory sliding-window limiter (single API process)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_s: float) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(self._hits) > 10_000:  # bound memory
            for k in list(self._hits)[:5_000]:
                if not self._hits[k]:
                    del self._hits[k]
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
