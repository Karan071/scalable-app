"""Shared harness for system tests: real service processes, one test env.

System tests run services as separate `uvicorn` processes (every service's
package is named `app`, so two can't share one interpreter) against the
isolated test environment from `make test-env`. No production code changes:

- A fake OIDC IdP (JWKS + client-credentials token endpoint) stands in for
  Keycloak. Services validate its tokens exactly as they would Keycloak's,
  because issuer and JWKS URI are ordinary settings.
- Telemetry export is disabled (OTEL_SDK_DISABLED) so test processes never
  report into the dev Collector under dev service names.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = REPO_ROOT / ".local" / "system-tests"

TEST_DB = "scaleforge_test"
TEST_VHOST = "scaleforge-test"
TEST_REDIS_CACHE_URL = "redis://localhost:6380/1"
# Invitation tokens are encrypted with this key so the harness can read them
# back from the outbox, the only place they exist besides the invitee's email.
INVITATION_KEYS = {"k1": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="}

_ISSUER = "http://fake-idp.test/realms/scaleforge"
_KID = "system-test-key"


def _dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


ENV = _dotenv()


class FakeIdP:
    """JWKS + client-credentials token endpoint on 127.0.0.1.

    ``token_delay_s`` makes the token endpoint answer slowly, standing in for
    a degraded Keycloak (LLD-13 FS-3, slow dependency).
    """

    def __init__(self, *, token_delay_s: float = 0.0) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # Live view of callers stuck on the token endpoint (unlike RabbitMQ's
        # management stats, which refresh only every few seconds).
        self._lock = threading.Lock()
        self.tokens_in_flight = 0
        self.max_tokens_in_flight = 0
        jwk = json.loads(RSAAlgorithm(RSAAlgorithm.SHA256).to_jwk(self._key.public_key()))
        jwk.update(kid=_KID, use="sig", alg="RS256")
        jwks_body = json.dumps({"keys": [jwk]}).encode()
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib API)
                self._reply(jwks_body)

            def do_POST(self) -> None:  # noqa: N802
                with idp._lock:
                    idp.tokens_in_flight += 1
                    idp.max_tokens_in_flight = max(idp.max_tokens_in_flight, idp.tokens_in_flight)
                try:
                    time.sleep(token_delay_s)
                finally:
                    with idp._lock:
                        idp.tokens_in_flight -= 1
                form = parse_qs(self.rfile.read(int(self.headers["content-length"])).decode())
                client_id = form["client_id"][0]
                self._reply(
                    json.dumps(
                        {"access_token": idp.service_token(client_id), "expires_in": 300}
                    ).encode()
                )

            def _reply(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        host, port = self._httpd.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        self.issuer = _ISSUER

    def stop(self) -> None:
        self._httpd.shutdown()

    def _sign(self, claims: dict[str, object]) -> str:
        now = int(time.time())
        claims = {"iss": self.issuer, "iat": now, "exp": now + 3600, **claims}
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": _KID})

    def user_token(self, sf_uid: UUID, *, email: str) -> str:
        # acr 2 + fresh auth_time: platform routes require step-up (LLD-02 §6).
        return self._sign(
            {
                "sub": f"kc-{sf_uid}",
                "aud": "scaleforge-api",
                "sf_uid": str(sf_uid),
                "email": email,
                "email_verified": True,
                "acr": "2",
                "auth_time": int(time.time()),
            }
        )

    def service_token(self, client_id: str) -> str:
        return self._sign(
            {
                "sub": f"service-account-{client_id}",
                "aud": "scaleforge-internal",
                "scope": "orgs:read users:read",
            }
        )


def service_env(service: str, idp: FakeIdP, **overrides: str) -> dict[str, str]:
    short = service.removesuffix("-svc")
    env = {
        **os.environ,
        "SF_SERVICE_NAME": service,
        "SF_ENVIRONMENT": "system-test",
        "SF_DB_NAME": TEST_DB,
        "SF_DB_USER": f"svc_{short}",
        "SF_DB_PASSWORD": ENV["POSTGRES_APP_PASSWORD"],
        "SF_AUTHZ_READER_DB_PASSWORD": ENV["POSTGRES_APP_PASSWORD"],
        "SF_OIDC_ISSUER": idp.issuer,
        "SF_OIDC_JWKS_URI": f"{idp.base_url}/jwks",
        "SF_OIDC_TOKEN_ENDPOINT": f"{idp.base_url}/token",
        "SF_REDIS_CACHE_URL": TEST_REDIS_CACHE_URL,
        "SF_RABBITMQ_VHOST": TEST_VHOST,
        "SF_RABBITMQ_USER": ENV["SF_RABBITMQ_USER"],
        "SF_RABBITMQ_PASSWORD": ENV["SF_RABBITMQ_PASSWORD"],
        "SF_INVITATION_ENC_KEYS": json.dumps(INVITATION_KEYS),
        "SF_INVITATION_ENC_PRIMARY_KID": "k1",
        # Unused by these flows (no MFA-required role is granted), but required.
        "SF_KEYCLOAK_ADMIN_BASE_URL": f"{idp.base_url}/admin/realms/scaleforge",
        "SF_KEYCLOAK_ADMIN_CLIENT_SECRET": "unused",
        "SF_SVC_AUTHZ_CLIENT_SECRET": "unused",
        "OTEL_SDK_DISABLED": "true",
    }
    env.update(overrides)
    return env


class ServiceProcess:
    """One `uvicorn` process for a service, logging to .local/system-tests/."""

    def __init__(self, service: str, *, port: int, env: dict[str, str], label: str = "") -> None:
        self.service = service
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._env = env
        self._log_path = LOG_DIR / f"{label or service}.log"
        self._proc: subprocess.Popen[bytes] | None = None

    async def start(self, *, ready_timeout_s: float = 60.0) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = self._log_path.open("wb")
        self._proc = subprocess.Popen(
            [
                "uv",
                "run",
                "--quiet",
                "--project",
                ".",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=REPO_ROOT / "services" / self.service,
            env=self._env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + ready_timeout_s
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    raise RuntimeError(f"{self.service} exited early; see {self._log_path}")
                with contextlib.suppress(httpx.HTTPError):
                    if (await client.get(f"{self.base_url}/readyz")).status_code == 200:
                        return
                await asyncio.sleep(0.25)
        raise TimeoutError(f"{self.service} not ready in {ready_timeout_s}s; see {self._log_path}")

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def bootstrap_super_user(user_id: UUID) -> None:
    """LLD-02 §11.6's own bootstrap CLI. Needs a fresh test DB (`make test-env`)."""
    subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "--project",
            ".",
            "python",
            "-m",
            "app.cli",
            "bootstrap-super-user",
            "--user-id",
            str(user_id),
        ],
        cwd=REPO_ROOT / "services" / "authz-svc",
        env={**os.environ, "SF_DB_NAME": TEST_DB, "SF_DB_PASSWORD": ENV["POSTGRES_APP_PASSWORD"]},
        check=True,
    )


async def wait_until(
    check: Callable[[], Awaitable[bool]], *, timeout_s: float, interval_s: float = 0.025
) -> float:
    """Polls ``check`` until true; returns the seconds it took."""
    started = time.monotonic()
    while True:
        if await check():
            return time.monotonic() - started
        if time.monotonic() - started > timeout_s:
            raise TimeoutError(f"condition not met within {timeout_s}s")
        await asyncio.sleep(interval_s)
