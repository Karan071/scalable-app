"""Synchronizer-token CSRF protection (LLD-01 §7, SEC-04, FR-AUTH-06)."""

import base64
import hashlib
import hmac

_INFO = b"csrf-v1"


def derive_csrf_token(csrf_secret: str) -> str:
    digest = hmac.new(csrf_secret.encode(), _INFO, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode()


def verify_csrf_token(csrf_secret: str, presented_token: str) -> bool:
    return hmac.compare_digest(derive_csrf_token(csrf_secret), presented_token)
