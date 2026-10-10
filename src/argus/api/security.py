"""Dashboard auth — pure, FastAPI-free (testable without network).

Passwords are hashed with **stdlib ``hashlib.scrypt``** (no extra dependency) and verified
with a constant-time comparison. The user table comes from ``.env``
(``ARGUS_DASHBOARD_USERS``) in ``name:scrypt$...`` form — **passwords never live in plain
text or in code**.

The session is an HMAC-signed token (``user.expiry.signature``); no server-side state is
needed (sufficient for a small set of authorized users). Read-only, session-based, and no
secrets leak.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

# scrypt parameters (around the RFC 7914 interactive recommendation; enough for a
# small cockpit).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_DKLEN = 32


def hash_password(password: str) -> str:
    """Hashes a password as ``scrypt$N$r$p$salt_hex$hash_hex`` (setup helper)."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Verifies a password against an encoded hash in **constant time**; False if malformed."""
    try:
        scheme, n_s, r_s, p_s, salt_hex, hash_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def parse_users(raw: str) -> dict[str, str]:
    """``"name:hash,name2:hash2"`` → ``{name: hash}``. Empty/malformed entries are skipped.

    The hash itself contains ``$``, so only the **first** ``:`` counts as the separator.
    """
    users: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        name, encoded = entry.split(":", 1)
        name = name.strip()
        if name and encoded:
            users[name] = encoded.strip()
    return users


def authenticate(users: dict[str, str], username: str, password: str) -> bool:
    """Whether user + password match the stored hash (constant time, unknown users too)."""
    encoded = users.get(username)
    if encoded is None:
        # Still run one (dummy) scrypt to reduce timing leakage.
        verify_password(password, "scrypt$1$1$1$00$00")
        return False
    return verify_password(password, encoded)


@dataclass(frozen=True)
class SessionConfig:
    """Configuration for issuing/verifying session tokens."""

    secret: bytes
    ttl_seconds: int = 12 * 3600  # 12 hours

    @classmethod
    def from_secret(cls, secret: str, *, ttl_seconds: int = 12 * 3600) -> SessionConfig:
        """With an empty secret, uses a random per-process key (sessions drop on restart)."""
        key = secret.encode() if secret else secrets.token_bytes(32)
        return cls(secret=key, ttl_seconds=ttl_seconds)


def issue_session(cfg: SessionConfig, username: str, *, now: float | None = None) -> str:
    """Issues a signed session token of the form ``username.expiry.hmac``."""
    expiry = int((now if now is not None else time.time()) + cfg.ttl_seconds)
    payload = f"{username}.{expiry}"
    sig = _sign(cfg.secret, payload)
    return f"{payload}.{sig}"


def verify_session(cfg: SessionConfig, token: str, *, now: float | None = None) -> str | None:
    """Returns the username if the token is valid and unexpired, otherwise None."""
    try:
        username, expiry_s, sig = token.rsplit(".", 2)
    except ValueError:
        return None
    payload = f"{username}.{expiry_s}"
    if not hmac.compare_digest(sig, _sign(cfg.secret, payload)):
        return None
    try:
        expiry = int(expiry_s)
    except ValueError:
        return None
    current = now if now is not None else time.time()
    if current >= expiry:
        return None
    return username


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
