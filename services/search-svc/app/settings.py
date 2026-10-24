"""search-svc settings (SF_* env vars, HLD §11.4: fail fast on invalid config).

No OIDC/Casbin settings: every route here is ``Public`` (LLD-05 §8 — search
is unauthenticated browse), so there's no token to validate and no local
enforcer to load. The reindex CLI (``app.cli``) needs catalog-svc's base URL
and a client-credentials secret of its own, read directly rather than
threaded through this class — it's a background Job, not the live app.
"""

from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "scaleforge"
    db_user: str = "svc_search"
    db_password: str

    # Inbox consumer for catalog-svc's product.* events (LLD-08 §6, E3.2).
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_vhost: str = "/"
    rabbitmq_user: str
    rabbitmq_password: str

    # HMAC key for keyset pagination cursors (sf_db.encode_cursor/decode_cursor).
    cursor_secret_b64: str
