import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://kc.test/realms/scaleforge"
KID = "test-key-1"


class _JWKSServer:
    def __init__(self, jwk: dict[str, str]) -> None:
        body = json.dumps({"keys": [jwk]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib API)
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/jwks"

    def start(self) -> None:
        self._thread.start()
        time.sleep(0.05)

    def stop(self) -> None:
        self._httpd.shutdown()


@pytest.fixture(scope="session")
def rsa_key_pair() -> tuple[object, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="session")
def jwks_server(rsa_key_pair: tuple[object, object]) -> Iterator[str]:
    _, public_key = rsa_key_pair
    jwk = json.loads(RSAAlgorithm(RSAAlgorithm.SHA256).to_jwk(public_key))
    jwk["kid"] = KID
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"

    server = _JWKSServer(jwk)
    server.start()
    yield server.url
    server.stop()


@pytest.fixture
def sign_token(rsa_key_pair: tuple[object, object]):
    private_key, _ = rsa_key_pair

    def _sign(claims: dict[str, object], *, kid: str = KID) -> str:
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})

    return _sign
