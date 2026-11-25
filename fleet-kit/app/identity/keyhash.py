"""Hash-only credential storage (B2).

The Hub never holds or compares a plaintext API key. Every configured key is
one of these hash forms:

    sha256:<64 hex>        SHA-256 of the key (UTF-8)
    hmac-sha256:<64 hex>   HMAC-SHA256 of the key, keyed with the server-side
                           pepper from AI_FLEET_KEY_PEPPER

Why no slow KDF (bcrypt/scrypt/argon2): the keys are machine-generated with
at least 256 bits of entropy (scripts/gen_keys.py), so a leaked SHA-256 hash
cannot be inverted by guessing; a slow KDF only helps low-entropy secrets
such as passwords, and would cost every request. The HMAC form adds a second
secret: a leaked key configuration alone is then not even enough to test a
guess offline.

A presented key is hashed under each scheme and compared against EVERY
configured entry with hmac.compare_digest, without stopping at the first
match, so the time taken does not depend on which entry (if any) matched.

Pure stdlib (hashlib, hmac, re); no FastAPI imports.
"""
import hashlib
import hmac
import re

SCHEME_SHA256 = "sha256"
SCHEME_HMAC = "hmac-sha256"
PEPPER_ENV = "AI_FLEET_KEY_PEPPER"
MIN_PEPPER_LENGTH = 32

_HASHED = re.compile(r"^(sha256|hmac-sha256):([0-9a-fA-F]{64})$")
_NO_MATCH = object()


def is_hashed(value) -> bool:
    """True when value is in one of the stored hash forms."""
    return isinstance(value, str) and bool(_HASHED.match(value))


def normalize(value: str) -> str:
    """Canonical form of a hashed entry (lower-case hex)."""
    scheme, digest = value.split(":", 1)
    return f"{scheme}:{digest.lower()}"


def scheme_of(value: str) -> str:
    return value.split(":", 1)[0]


def hash_key(key: str, pepper: str = "") -> str:
    """The stored form of key: hmac-sha256 when a pepper is given, else sha256."""
    raw = key.encode("utf-8")
    if pepper:
        digest = hmac.new(pepper.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        return f"{SCHEME_HMAC}:{digest}"
    return f"{SCHEME_SHA256}:{hashlib.sha256(raw).hexdigest()}"


def hash_key_map(mapping: dict, pepper: str = "") -> dict:
    """{key: value} -> {hash_key(key): value}, for building the JSON maps of
    AI_FLEET_TENANT_API_KEYS / AI_FLEET_AGENT_API_KEYS (scripts and tests)."""
    return {hash_key(k, pepper): v for k, v in mapping.items()}


def presented_hashes(presented: str, pepper: str = "") -> dict:
    """scheme -> stored form of the presented key, for every usable scheme.
    The hmac form exists only when the pepper is configured."""
    out = {SCHEME_SHA256: hash_key(presented)}
    if pepper:
        out[SCHEME_HMAC] = hash_key(presented, pepper)
    return out


def find(presented: dict, entries) -> object:
    """Value of the configured entry that matches, or None.

    presented: presented_hashes(...) of the presented key.
    entries: iterable of (stored_hash, value); stored_hash must be normalized.
    Every entry is compared (no early exit), each in constant time.
    """
    found = _NO_MATCH
    for stored, value in entries:
        candidate = presented.get(scheme_of(stored), "")
        ok = hmac.compare_digest(candidate.encode("ascii"), stored.encode("ascii"))
        if ok and found is _NO_MATCH:
            found = value
    return None if found is _NO_MATCH else found
