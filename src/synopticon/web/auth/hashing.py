"""Password/token hashing primitives shared across the auth package.

Stdlib-only. scrypt work factors are the interactive-login parameters
recommended for the algorithm (n=2**14, r=8, p=1) -- a good balance for a
self-hosted single-admin GUI without pulling in a password-hashing dependency.
"""

from __future__ import annotations

import hashlib

# scrypt work factors. n must be a power of two.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

# maxmem must be large enough for the chosen (n, r, p); the default 32 MiB is not.
_SCRYPT_MAXMEM = 128 * _SCRYPT_R * _SCRYPT_N * 2


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
