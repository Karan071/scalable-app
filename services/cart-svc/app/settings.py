"""cart-svc settings (SF_* env vars, HLD §11.4: fail fast on invalid config)."""

from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "scaleforge"
    db_user: str = "svc_cart"
    db_password: str

    oidc_issuer: str
    oidc_jwks_uri: str
    oidc_token_endpoint: str

    # Embedded Casbin enforcer (ADR-0005: every service enforces locally).
    # Every route here is domain="customer" (LLD-06 §2), but still a real
    # Authorize() check -- not AuthenticatedUser() -- so a user who never
    # got role:customer (shouldn't happen: granted on signup, LLD-02 §9) is
    # refused rather than silently allowed.
    authz_reader_db_user: str = "authz_reader"
    authz_reader_db_password: str
    redis_cache_url: str = "redis://localhost:6380/0"

    # Add-to-cart validates the SKU against catalog-svc (LLD-06 §2), synchronously.
    catalog_svc_base_url: str
    svc_cart_client_id: str = "sf-svc-cart"
    svc_cart_client_secret: str
