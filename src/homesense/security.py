"""Password hashing (stdlib scrypt) and startup security checks."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from homesense.core.config import Settings

_ALGO = "scrypt"
_N, _R, _P, _DKLEN = 2**14, 8, 1, 64
_SALT_BYTES = 16
_PLACEHOLDER_SECRET = "change-me"


def _scrypt(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)


def hash_password(pw: str) -> str:
    """Returns "scrypt$<salt_hex>$<hash_hex>"."""
    salt = secrets.token_bytes(_SALT_BYTES)
    return f"{_ALGO}${salt.hex()}${_scrypt(pw, salt).hex()}"


def verify_password(pw: str, stored: str) -> bool:
    """Constant-time check of `pw` against a stored hash; False for any malformed hash."""
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except ValueError:
        return False
    if not salt or not expected:
        return False
    return hmac.compare_digest(_scrypt(pw, salt), expected)


def session_fingerprint(secret_key: str, password_hash: str) -> str:
    """HMAC-SHA256 of the stored password hash: sessions die when the password changes or the key rotates."""
    return hmac.new(secret_key.encode("utf-8"), password_hash.encode("utf-8"), hashlib.sha256).hexdigest()


def check_secret_key(settings: Settings) -> list[str]:
    """Problems with the session secret key (empty list when it is usable)."""
    key = settings.secret_key.strip()
    if not key:
        return ["HOMESENSE_SECRET_KEY is empty; set a long random value in .env"]
    if key == _PLACEHOLDER_SECRET:
        return [f"HOMESENSE_SECRET_KEY is still the placeholder {_PLACEHOLDER_SECRET!r}; set a random value"]
    return []
