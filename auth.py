"""Password hashing and session tokens.

Email + password, not a PIN — see CLAUDE.md for why. stdlib only
(hashlib.pbkdf2_hmac), no extra dependency for something this security
sensitive.
"""
import hashlib
import hmac
import os
import secrets

_ITERATIONS = 600_000  # OWASP's current minimum recommendation for PBKDF2-SHA256


def hash_password(password: str) -> str:
    """Returns 'salt_hex$hash_hex', safe to store in people.password_hash."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison — never use == on password hashes."""
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return hmac.compare_digest(actual, expected)


def generate_temp_password() -> str:
    """For owner-initiated resets: readable, not easily mistyped (no 0/O/1/l/I)."""
    alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
