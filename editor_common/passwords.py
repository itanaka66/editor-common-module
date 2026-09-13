"""Password hashing for the multi-user auth store, stdlib-only (PBKDF2-HMAC
via `hashlib`, no bcrypt/passlib dependency needed).

Stored format: ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`` — the
iteration count and salt travel with the hash, so it can be verified without
any external config and the work factor can be raised later without
invalidating hashes made with a lower one.
"""
import hashlib
import hmac
import os

ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 260_000
SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations_s, salt_hex, hash_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != ALGORITHM:
        return False
    try:
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str, *, iterations: int = DEFAULT_ITERATIONS) -> bool:
    """True if `stored` was hashed with a lower iteration count than the
    current default — call after a successful `verify_password` and
    re-save with a fresh `hash_password` if this returns True."""
    try:
        algorithm, iterations_s, _, _ = stored.split("$", 3)
        return algorithm != ALGORITHM or int(iterations_s) < iterations
    except ValueError:
        return True
