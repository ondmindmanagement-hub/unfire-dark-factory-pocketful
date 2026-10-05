"""Password hashing with scrypt (stdlib only).

Hashes are `scrypt$<salt-hex>$<digest-hex>`. Verification results are memoised
per (encoded hash, password digest) so repeated logins against the same account
do not each pay the full key-derivation cost; the cache never stores plaintext.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading

_N, _R, _P = 16384, 8, 1
_MAXMEM = 64 * 1024 * 1024

_verified: dict[tuple[str, str], bool] = {}
_verified_lock = threading.Lock()


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
                          maxmem=_MAXMEM)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${_derive(password, salt).hex()}"


def verify_password(password, encoded) -> bool:
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    memo_key = (encoded, hashlib.sha256(password.encode("utf-8")).hexdigest())
    with _verified_lock:
        cached = _verified.get(memo_key)
    if cached is not None:
        return cached
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        ok = hmac.compare_digest(_derive(password, bytes.fromhex(salt)).hex(), expected)
    except (ValueError, TypeError, AttributeError):
        return False
    with _verified_lock:
        if len(_verified) > 10000:
            _verified.clear()
        _verified[memo_key] = ok
    return ok


def looks_hashed(encoded) -> bool:
    if not isinstance(encoded, str):
        return False
    parts = encoded.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    try:
        bytes.fromhex(parts[1])
        bytes.fromhex(parts[2])
    except ValueError:
        return False
    return True
