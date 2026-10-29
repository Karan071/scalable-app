# LLD-15: Frontend (React SPA) and BFF Integration

| Field | Value |
| --- | --- |
| Status | Draft v0.1 (M2 shell/auth/org-console/platform-admin built 2026-10-22; M3 storefront/checkout/commerce-admin built 2026-10-25 — see §11's own "As built") |
| Milestone | M2 (shell, auth, org console, platform admin) → M3 (storefront, checkout, commerce admin) → M4 (SSE, ops console) |
| Stack | React, TypeScript, Vite, TanStack Query, React Router |

## 1. Scope

This LLD covers:

- frontend structure;
- the authentication and session handling contract with the BFF;
- data fetching and organization switching;
- debounced search;
- rendering safety;
- real-time updates;
- error UX;
- frontend telemetry and tests.

## 2. Structure

| Area | Route prefix | Audience | Bundle |
| --- | --- | --- | --- |
| Storefront | `/s/:storeSlug/*` | Public + customers | Main |
| Account | `/account/*` (orders, profile, notifications) | Customers | Lazy |
| Organization console | `/console/orgs/:orgId/*` (catalog, inventory, orders, members, invitations, roles, audit, settings) | Org staff | Lazy |
| Platform admin | `/admin/*` (users, orgs, platform roles, audit) | Platform roles | Lazy |
| Operations console | `/ops/*` (DLQ, reconciliation, flags status, dashboard links) | platform_operator / super_user | Lazy |

- One SPA, served as static assets by `web` on the **same origin** as the BFF (`/auth`, `/api`). Same origin keeps cookies first-party and makes CSRF and CORS simple: CORS is disabled entirely.
- Code splitting per area reduces the customer bundle. Hiding admin code is **not** a security control (PRD §3.3).
- Navigation guards use `/auth/session` permission hints for UX only. The server decides.

## 3. Authentication and session handling (FR-AUTH-03)

| Concern | Behavior |
| --- | --- |
| Bootstrap | On load: `GET /auth/session` gives the authenticated flag, user summary, memberships, platform roles, the CSRF token, and `acr` |
| Login | Full-page navigation to `/auth/login?return_to=<current path>`. The SPA never handles credentials or tokens. |
| CSRF | Token held **in memory only** (module state). The API client adds `X-CSRF-Token` to unsafe methods. Re-fetched after login, step-up, and any 403 `csrf-invalid` (one retry). |
| Storage rule | No auth material in `localStorage`, `sessionStorage`, IndexedDB, or JS-readable cookies. Allowed in storage: UI preferences, and the checkout idempotency key (§4). |
| 401 `session_expired` / `unauthenticated` | Clear the query cache → navigate to login with `return_to` |
| 401 `step_up_required` | Navigate to `step_up_url` (`/auth/step-up?return_to=`). After return, the action is re-attempted by the user, not replayed automatically. |
| 503 `session_store_unavailable` | Banner "Reconnecting…". Automatic retry for GETs with backoff. Mutations are not auto-retried. |
| Logout | `POST /auth/logout` with CSRF → follow the redirect to the Keycloak end-session endpoint |

## 4. Data fetching and API client

- **API client:** a thin wrapper over `fetch`:
  - `credentials: 'same-origin'`;
  - JSON only;
  - parses RFC 9457 problems into typed errors;
  - attaches CSRF;
  - honors `Retry-After` for 429 and 503 on idempotent requests only.
- **TanStack Query:**
  - Query keys always include the tenant: `['org', orgId, 'products', filters]`.
  - Switching orgs (FR-ORG-05) is a navigation to another `:orgId` that removes queries for other orgs and re-fetches permission hints for the new domain.
  - No cross-org data appears from cache.
- **Optimistic concurrency:** admin edit forms keep the `ETag` and send `If-Match`. On 412, show "This item changed" with a diff/reload option.
- **Checkout:**
  - The idempotency key is generated with `crypto.randomUUID()` when the checkout page mounts, and kept in `sessionStorage` keyed by cart ID, so a reload reuses it.
  - The submit button is disabled while pending.
  - Network retries reuse the key.
  - A 202 response navigates to the order status page, which subscribes to SSE.

## 5. Debounced search (FR-COM-02, EXP-5)

| Rule | Value |
| --- | --- |
| Debounce | 300 ms after the last keystroke |
| Minimum length | 2 characters (empty query = browse) |
| Cancellation | `AbortController` cancels the in-flight request when a new one starts |
| Stale responses | Ignored if the query has changed (key-based) |
| UX | Keep previous results visible while loading (`placeholderData: keepPreviousData`) |
| URL sync | `q`, filters, and sort in the URL query string, for shareable and back-button-friendly results |
| Validation | Client mirrors server limits (≤ 100 chars). The server remains authoritative. |
| Measurement | Playwright typing script counts requests per typed query with and without debounce |

## 6. Rendering safety (SEC-05)

- **React's default escaping.** `dangerouslySetInnerHTML` is banned by an ESLint rule, except inside `<SafeHtml>`. That component runs DOMPurify with a strict allow-list matching the server sanitizer (LLD-05 §2), and it is used only for product descriptions.
- **URLs** from data (images, links) are validated: `https:` or same-origin relative only. `javascript:` and `data:` links are rejected.
- **CSP** is enforced (LLD-11 §4.2). There are no inline scripts or styles; the build emits hashed assets.
- **Third-party scripts:** none in v1.

## 7. Real-time updates

- `EventSource('/api/events')` is opened only when authenticated, for the order status and notifications views. The cookie authenticates it.
- **Events:** `order.status_changed` → invalidate `['me', 'orders', orderId]`; `notification.in_app` → increment the badge and invalidate the notifications list.
- **Fallback:** when `EventSource` errors repeatedly (> 3 within 1 min), switch to long-polling `GET /api/orders/{id}/status?wait=25` on the order page.

## 8. Error UX mapping

| Problem type | UX |
| --- | --- |
| `validation-error` (422/400) | Field-level messages from `errors[]` |
| `forbidden` (403) | "You don't have access" page with an org switcher. No resource details. |
| `org-suspended` (403) | Banner: "This organization is suspended" |
| `not-found` (404) | Standard not-found (also for foreign IDs) |
| `precondition-failed` (412) | Concurrent-edit dialog |
| `out-of-stock`, `cart-changed`, `payment-declined` (409/402) | Checkout-specific guidance; cart refresh |
| `rate-limited` (429) | Toast with retry time |
| `dependency-unavailable`, `deadline-exceeded` (503) | Retry banner; the correlation ID is shown for support |

## 9. Frontend telemetry

- **Web Vitals** (LCP, INP, CLS) and route-change timings are sent to `/api/telemetry` (sampled 10%, rate-limited).
- **Optional OTel Web SDK** (M6): fetch spans with `traceparent` to same-origin only; URLs stripped of query strings; no user input recorded (LLD-10 §6).
- Errors are captured with stack traces (source maps are uploaded privately, not served publicly), but no user data.

## 10. Tests

| Test | Type |
| --- | --- |
| API client: CSRF header on unsafe methods; problem parsing; 401/step-up/429/503 flows | Unit (Vitest + MSW) |
| Query keys include the org; switching orgs clears other orgs' data | Unit |
| `<SafeHtml>` neutralizes the XSS corpus; ESLint rule blocks raw usage | Unit + lint |
| Debounce/cancel behavior (fake timers) | Unit |
| Token-free browser: after login, inspect all storage and cookies | E2E (Playwright) |
| Login → browse → search → cart → checkout → order status (SSE) | E2E |
| Org console: invite → accept (second browser context) → assign role → member acts → revoke → access lost | E2E |
| Platform admin: step-up required for privileged actions | E2E |
| Accessibility smoke (axe) on key pages | E2E |

**As built (storefront/checkout/commerce-admin, 2026-10-25, E3.7).** `/s/:orgId/*` (not `:storeSlug` — no slug→org_id resolver service exists), `/account/*` (orders only; profile/notifications aren't built), and `/console/orgs/:orgId/{catalog,inventory,orders}` (new tabs alongside M2's members/invitations/roles). §4's idempotency key and §5's debounce (300ms, 2-char minimum, `placeholderData: keepPreviousData`, query-key-based staleness) are built as specified. §4's "BFF composition" for product detail isn't — the BFF has only the generic authenticated reverse proxy (§6 above), no per-page composition route, so `ProductDetailPage` merges catalog-svc and inventory-svc client-side with two parallel requests instead (same degraded-on-failure behavior the LLD specifies). §6's `<SafeHtml>`/DOMPurify layer isn't built — product descriptions render via `dangerouslySetInnerHTML` directly, relying on catalog-svc's own server-side sanitizer (LLD-05 §2) as the only layer. §7's SSE isn't built (M4, per this LLD's own milestone column); the order status page uses the §7 fallback (polling, 2s, while `status === "pending"`) as its only mechanism. §5's category/price/sort filters aren't wired into the browse page, only `q`. §9's telemetry and the OTel Web SDK aren't built. §10's axe accessibility suite still isn't built. §10's Playwright suite now has one real spec (`tests/system/test_checkout_browser.py`, 2026-10-28): login → browse → add-to-cart → checkout → confirmed through a genuine Chromium browser and the real Compose Keycloak — superseding this note's own "abandoned" line below. The rest of §10 still leans on Vitest + React Testing Library + MSW (real component rendering against mocked network calls): the API client, the debounce hook, `error-ux`'s conflict-title mapping, `BrowsePage`'s debounce end-to-end, and `CartPage`'s catalog-enriched subtotal.

**§11: the real-browser login gap, closed 2026-10-28.** The BFF now has a static/SPA-fallback route (`services/bff/app/spa.py`, opt-in via `SF_WEB_DIST_DIR`) that serves this package's own `dist/` and falls back to `index.html` for client-side routes — local-dev-only; Kubernetes still routes static assets to a separate `web` gateway backend (LLD-12 §6), unaffected. Driving this through a real browser for the first time (rather than `tests/system/keycloak.py`'s own HTTP-level `Browser`, which fakes rendering) also found checkout failing unconditionally through the BFF: `services/bff/app/proxy.py`'s generic `/api/**` forward dropped every header beyond `Authorization`/correlation/`traceparent`, including `Idempotency-Key` (§4) and every `If-Match` this package's own API clients send — fixed with an explicit allow-list, not a simplification (see `docs/00-delivery-plan.md`'s M3 exit-evidence note for the full detail).

## 11. Requirements covered

FR-COM-01/02 (UI), FR-CHK-01/06 (UI), FR-ORG-05, FR-AUTH-03; SEC-04 (client side), SEC-05; PRD §5.2 debounced search; PRD §7.3 browser tracing privacy.
