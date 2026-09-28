"""Real Web Push crypto: VAPID JWT + aes128gcm payload encryption, decrypted as a browser would."""

from __future__ import annotations

import base64
import json
import os

import http_ece
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import Settings
from app.db import models as M
from app.notifications.service import PushSender


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class CaptureSession:
    def __init__(self) -> None:
        self.req: dict | None = None

    def post(self, url, data=None, headers=None, timeout=None):  # noqa: ANN001, ANN201
        self.req = {"url": url, "data": data, "headers": headers}

        class R:
            status_code = 201
            text = ""
            headers: dict = {}

        return R()


async def test_real_vapid_signed_encrypted_push(monkeypatch):
    # server VAPID key pair (same format as `python -m app.cli gen-vapid`)
    vk = ec.generate_private_key(ec.SECP256R1())
    vapid_priv = b64u(vk.private_numbers().private_value.to_bytes(32, "big"))
    vapid_pub = b64u(vk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    # browser subscription key pair + auth secret
    ua = ec.generate_private_key(ec.SECP256R1())
    ua_pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    sub = M.PushSubscription(endpoint="https://web.push.apple.com/QGuQ-example", p256dh=b64u(ua_pub), auth=b64u(auth))

    settings = Settings(auth_secret="x" * 40, vapid_public_key=vapid_pub, vapid_private_key=vapid_priv,
                        vapid_subject="https://example.com")
    sess = CaptureSession()
    import pywebpush

    real = pywebpush.webpush
    monkeypatch.setattr(pywebpush, "webpush", lambda **kw: real(**kw, requests_session=sess))
    payload = {"title": "BTC LONG opened", "body": "BTC LONG opened at $84,750", "url": "/positions"}
    ok, code, err = await PushSender(settings).send(sub, payload, ttl=3600, urgency="high")
    assert ok and code == 201, err

    req = sess.req
    assert req["url"] == sub.endpoint
    h = {k.lower(): v for k, v in req["headers"].items()}
    assert h["content-encoding"] == "aes128gcm" and h["ttl"] == "3600" and h["urgency"] == "high"
    # VAPID: "vapid t=<jwt>, k=<public key>"
    auth_hdr = h["authorization"]
    assert auth_hdr.startswith("vapid t=") and f"k={vapid_pub}" in auth_hdr
    jwt = auth_hdr.split("t=")[1].split(",")[0]
    claims = json.loads(unb64u(jwt.split(".")[1]))
    assert claims["aud"] == "https://web.push.apple.com" and claims["sub"] == "https://example.com"
    # verify the ES256 signature with the VAPID public key
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    head, body, sig = jwt.split(".")
    raw = unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    vk.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    # decrypt exactly as the user agent does
    plain = http_ece.decrypt(req["data"], private_key=ua, auth_secret=auth, version="aes128gcm")
    assert json.loads(plain) == payload
