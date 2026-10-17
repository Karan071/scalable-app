"""Outbound HTTP client with explicit timeouts (HLD §9.1).

Bounded retries, circuit breakers, and bulkheads are resilience-library work for
M4 (LLD-13 §2); this factory only sets the deadline, which later wraps it.
"""

import httpx

DEFAULT_CONNECT_TIMEOUT_S = 1.0
DEFAULT_TOTAL_TIMEOUT_S = 3.0


def create_http_client(total_timeout_s: float = DEFAULT_TOTAL_TIMEOUT_S) -> httpx.AsyncClient:
    timeout = httpx.Timeout(total_timeout_s, connect=DEFAULT_CONNECT_TIMEOUT_S)
    return httpx.AsyncClient(timeout=timeout)
