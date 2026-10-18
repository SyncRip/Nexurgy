"""Encryption of direct model keys and fingerprints of environment keys (pure: no DB).

Direct keys (W1, docs/development/mechanical-engineering/credentials-deploy.zh-CN.md) are
encrypted with AES-256-GCM from the maintained ``cryptography`` package; nothing here is a
home-made cipher. The master keys come from the deployment environment::

    PARTROUTE_CREDENTIAL_KEYS="v2:<base64 of 32 random bytes>,v1:<base64 of 32 random bytes>"

The first entry encrypts new keys; every listed entry can decrypt (rotation keeps the old
ones listed until ``rekey`` moved every record to the current one). Without the variable,
direct keys can be neither saved nor used (``master_key_missing``); environment
references keep working.

Stored format (version 1): algorithm ``AES-256-GCM``, the master key id, a random 96-bit
nonce and the ciphertext with its 128-bit tag. The associated data binds the record to its
configuration and credential version, so a ciphertext copied to another row does not decrypt.

Environment keys are never copied into the database: a keyed fingerprint (HMAC-SHA256 with a
server-side secret) tells whether the value behind a reference changed. Fingerprints and key
values are never returned by an API, logged or put into messages.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .providers.base import ProviderError

KEYS_ENV = "PARTROUTE_CREDENTIAL_KEYS"
#: Optional extra secret mixed into environment-key fingerprints (base64, any length >= 16 bytes).
FINGERPRINT_ENV = "PARTROUTE_CREDENTIAL_FINGERPRINT_KEY"
ALGORITHM = "AES-256-GCM"
CIPHER_FORMAT = 1
NONCE_BYTES = 12
_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: Characters the settings form uses to show "a key is stored"; a key made of them is a mask, not a key.
MASK_CHARS = frozenset("•●*·…xX")
API_KEY_MIN, API_KEY_MAX = 8, 1000


@dataclass(frozen=True)
class Keyring:
    """Master keys by id; ``current`` encrypts. ``error`` is a description of a malformed variable
    (it never contains key material)."""

    current: str | None
    keys: dict[str, bytes] = field(default_factory=dict, repr=False)
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.current is not None and self.error is None


@lru_cache(maxsize=8)
def _parse(raw: str) -> Keyring:
    keys: dict[str, bytes] = {}
    order: list[str] = []
    for i, entry in enumerate(p.strip() for p in raw.split(",")):
        if not entry:
            continue
        kid, sep, b64 = entry.partition(":")
        kid = kid.strip()
        if not sep or not _KEY_ID.match(kid):
            return Keyring(None, {}, f"{KEYS_ENV} entry {i + 1} is not '<id>:<base64 key>'")
        try:
            key = base64.b64decode(b64.strip(), validate=True)
        except (binascii.Error, ValueError):
            return Keyring(None, {}, f"{KEYS_ENV} entry {i + 1} ({kid}) is not valid base64")
        if len(key) != 32:
            return Keyring(None, {}, f"{KEYS_ENV} entry {i + 1} ({kid}) must decode to 32 bytes")
        if kid in keys:
            return Keyring(None, {}, f"{KEYS_ENV} lists key id {kid} twice")
        keys[kid] = key
        order.append(kid)
    if not order:
        return Keyring(None, {}, None)
    return Keyring(order[0], keys, None)


def keyring() -> Keyring:
    """The master keys of this process (re-read from the environment on each call; parsing is cached)."""
    from ..config import get_settings

    get_settings()  # loads server/.env into the process environment (without overriding it)
    return _parse(os.environ.get(KEYS_ENV, "").strip())


def require_keyring() -> Keyring:
    ring = keyring()
    if ring.error:
        raise ProviderError("master_key_missing", f"the credential master key is misconfigured: {ring.error}", retryable=False)
    if ring.current is None:
        raise ProviderError("master_key_missing", f"no credential master key is configured on the server ({KEYS_ENV}); "
                            "direct API keys cannot be saved or used", retryable=False)
    return ring


def aad(config_id: str, credential_version: int) -> bytes:
    """Associated data: binds a ciphertext to its configuration and credential version."""
    return f"partroute/model-credential/v{CIPHER_FORMAT}/{config_id}/{credential_version}".encode()


@dataclass(frozen=True)
class Sealed:
    algorithm: str
    cipher_format: int
    key_version: str
    nonce: bytes
    ciphertext: bytes


def encrypt(plaintext: str, *, config_id: str, credential_version: int, ring: Keyring | None = None) -> Sealed:
    ring = ring or require_keyring()
    if not ring.available:
        require_keyring()  # raises the precise reason
    assert ring.current is not None
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(ring.keys[ring.current]).encrypt(nonce, plaintext.encode("utf-8"), aad(config_id, credential_version))
    return Sealed(ALGORITHM, CIPHER_FORMAT, ring.current, nonce, ct)


def decrypt(*, algorithm: str | None, cipher_format: int | None, key_version: str | None, nonce: bytes | None,
            ciphertext: bytes | None, config_id: str, credential_version: int, ring: Keyring | None = None) -> str:
    """The plaintext key, or ProviderError master_key_missing / decrypt_failed (never with key material)."""
    if not ciphertext or not nonce:
        raise ProviderError("credential_cleared", "this credential no longer has a stored key", retryable=False)
    if algorithm != ALGORITHM or cipher_format != CIPHER_FORMAT:
        raise ProviderError("decrypt_failed", f"the stored key uses an unknown format ({algorithm} v{cipher_format})", retryable=False)
    ring = ring or keyring()
    if ring.error:
        raise ProviderError("master_key_missing", f"the credential master key is misconfigured: {ring.error}", retryable=False)
    if not ring.keys:
        raise ProviderError("master_key_missing", f"no credential master key is configured on the server ({KEYS_ENV})", retryable=False)
    key = ring.keys.get(key_version or "")
    if key is None:
        raise ProviderError("decrypt_failed", f"the stored key was encrypted with master key {key_version}, which is not configured "
                            f"on this server ({KEYS_ENV})", retryable=False)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad(config_id, credential_version)).decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, ValueError):
        raise ProviderError("decrypt_failed", f"the stored key could not be decrypted with master key {key_version} "
                            "(wrong key material or altered record)", retryable=False) from None


def fingerprint(salt: bytes, ref: str, value: str) -> str:
    """Keyed, irreversible fingerprint of an environment key (hex). Server-side only."""
    pepper = os.environ.get(FINGERPRINT_ENV, "").encode()
    mac = hmac.new(salt + b"\x00" + pepper, ref.encode() + b"\x00" + value.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def check_api_key(raw: str) -> tuple[str | None, str | None]:
    """(key, None) for a plausible key, else (None, error code): api_key_empty / api_key_masked / api_key_invalid.
    Error codes only, never the value."""
    key = (raw or "").strip()
    if not key:
        return None, "api_key_empty"
    if any(c in MASK_CHARS for c in key) and (set(key) <= MASK_CHARS or "•" in key or "●" in key or "…" in key or "***" in key):
        return None, "api_key_masked"
    if not API_KEY_MIN <= len(key) <= API_KEY_MAX:
        return None, "api_key_invalid"
    if any(c.isspace() or not c.isprintable() for c in key):
        return None, "api_key_invalid"
    return key, None


def new_master_key() -> str:
    """A fresh base64 master key (for the deployment note / CLI; never stored by the application)."""
    return base64.b64encode(os.urandom(32)).decode("ascii")
