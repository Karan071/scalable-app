"""AES-256-GCM encryption with key rotation by ``kid`` (LLD-01 §5.2, §5.5; LLD-03 §6.3).

Format: ``kid:nonce_b64:ciphertext_b64``. The newest key encrypts; any listed
key decrypts, so a rotation adds a key, deploys, switches the primary, and
only removes the old key after every ciphertext it created has expired.
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class TokenCipher:
    def __init__(self, keys: dict[str, bytes], primary_kid: str) -> None:
        if primary_kid not in keys:
            raise ValueError(f"primary_kid {primary_kid!r} not in keys")
        self._keys = keys
        self._primary_kid = primary_kid

    @classmethod
    def from_base64_keys(cls, keys_b64: dict[str, str], primary_kid: str) -> "TokenCipher":
        return cls({kid: base64.b64decode(v) for kid, v in keys_b64.items()}, primary_kid)

    def encrypt(self, plaintext: str, *, aad: bytes) -> str:
        key = self._keys[self._primary_kid]
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), aad)
        nonce_b64 = base64.urlsafe_b64encode(nonce).decode()
        ciphertext_b64 = base64.urlsafe_b64encode(ciphertext).decode()
        return f"{self._primary_kid}:{nonce_b64}:{ciphertext_b64}"

    def decrypt(self, token: str, *, aad: bytes) -> str:
        kid, nonce_b64, ciphertext_b64 = token.split(":", 2)
        key = self._keys[kid]
        nonce = base64.urlsafe_b64decode(nonce_b64)
        ciphertext = base64.urlsafe_b64decode(ciphertext_b64)
        return AESGCM(key).decrypt(nonce, ciphertext, aad).decode()
