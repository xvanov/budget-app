"""Secret encryption (AES-256-GCM) for third-party tokens at rest.

Currently used for Plaid access tokens (``plaid_items.access_token_encrypted``).
NEVER store those plaintext.

This is a deliberate port of ``rental-management/src/lib/crypto/secrets.ts`` and
is byte-compatible with it, so the SAME ``ENCRYPTION_KEY`` works in both apps
and a token encrypted by either can be read by the other:

    key:   ENCRYPTION_KEY env var, base64-encoded 32 bytes (openssl rand -base64 32)
    wire:  v1:<iv_b64>:<authTag_b64>:<ciphertext_b64>
    nonce: 12 bytes (GCM standard)

Node's ``createCipheriv('aes-256-gcm')`` keeps the 16-byte auth tag separate
from the ciphertext, while Python's ``AESGCM`` appends it. The split/join in
``encrypt_secret``/``decrypt_secret`` is what reconciles the two — it is the
whole reason this file can talk to the TS implementation.

The key is read fresh on every call rather than cached at import time, so a
container that starts without ENCRYPTION_KEY set fails at the point of use with
a clear message instead of failing to boot.
"""
import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"
IV_BYTES = 12
KEY_BYTES = 32
TAG_BYTES = 16


class EncryptionError(RuntimeError):
    """Raised for missing/malformed keys and for tampered or unreadable payloads."""


def _get_key() -> bytes:
    raw = os.environ.get("ENCRYPTION_KEY")
    if not raw:
        raise EncryptionError(
            "ENCRYPTION_KEY is not set. Generate one with `openssl rand -base64 32` "
            "and add it to .env."
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise EncryptionError("ENCRYPTION_KEY must be valid base64") from exc
    if len(key) != KEY_BYTES:
        raise EncryptionError(
            f"ENCRYPTION_KEY must decode to {KEY_BYTES} bytes (got {len(key)}). "
            "Use `openssl rand -base64 32`."
        )
    return key


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a UTF-8 string, returning the versioned wire format."""
    key = _get_key()
    iv = os.urandom(IV_BYTES)
    sealed = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    # AESGCM appends the tag; Node expects it as its own field.
    ciphertext, tag = sealed[:-TAG_BYTES], sealed[-TAG_BYTES:]
    return ":".join(
        [
            VERSION,
            base64.b64encode(iv).decode(),
            base64.b64encode(tag).decode(),
            base64.b64encode(ciphertext).decode(),
        ]
    )


def decrypt_secret(payload: str) -> str:
    """Decrypt a value produced by encrypt_secret or by secrets.ts.

    Raises EncryptionError on format, key, or tamper failures — never returns
    partial plaintext.
    """
    parts = (payload or "").split(":")
    if len(parts) != 4 or parts[0] != VERSION:
        raise EncryptionError("decrypt_secret: unrecognized ciphertext format")
    _, iv_b64, tag_b64, data_b64 = parts
    key = _get_key()
    try:
        iv = base64.b64decode(iv_b64, validate=True)
        tag = base64.b64decode(tag_b64, validate=True)
        ciphertext = base64.b64decode(data_b64, validate=True)
    except Exception as exc:
        raise EncryptionError("decrypt_secret: payload is not valid base64") from exc
    try:
        # Reassemble the layout AESGCM expects: ciphertext || tag.
        return AESGCM(key).decrypt(iv, ciphertext + tag, None).decode("utf-8")
    except InvalidTag as exc:
        raise EncryptionError(
            "decrypt_secret: authentication failed — wrong key or tampered payload"
        ) from exc


def is_encryption_configured() -> bool:
    """True when ENCRYPTION_KEY is present and correctly sized (startup checks)."""
    try:
        _get_key()
        return True
    except EncryptionError:
        return False
