// E4.1 (LLD-14 §6): the Browse scenario (§6.1's own table, 60% share) --
// store home (categories) -> listing (search, no query) -> 2-4 product
// details, open model via constant-arrival-rate (§6.2).
//
// Every call goes through the BFF (apps/web's own api-client.js does the
// same): even these Public()-marked, unauthenticated-at-the-service-layer
// storefront routes need a live BFF session, since the BFF's proxy itself
// calls require_session on every /api/** request regardless of what the
// downstream route needs (confirmed directly building this script -- the
// first version skipped login entirely and got a uniform 401). So every
// VU logs in once, through the real Keycloak login form, exactly as a
// browser would (keycloak.py's own Browser.sign_in, reimplemented here in
// k6's own http module since k6 has no Python runtime) -- not once per
// iteration: LLD-14 §6.1 "login load is tested separately."
//
// Run: k6 run k6/browse.js
// Needs: scripts/perf/seed_browse_baseline.py already run, and
// catalog-svc/search-svc/users-svc/bff up on the ports that script's own
// --bff-base-url / the service env below point at (see docs/reports/perf/
// 2026-10-29-browse-baseline.md for the exact commands used).

import http from 'k6/http';
import { check, sleep } from 'k6';

const config = JSON.parse(open('../scripts/perf/.browse-baseline.json'));
const BASE = config.base_url;

export const options = {
  scenarios: {
    browse_smoke: {
      executor: 'constant-arrival-rate',
      // LLD-14 §6.3 "Smoke: 5 min, low rate".
      rate: 1,
      timeUnit: '1s',
      duration: '5m',
      preAllocatedVUs: 40,
      maxVUs: 60,
    },
  },
  thresholds: {
    // Provisional SLOs (LLD-14 §6.2) -- this run's own job is to find out
    // whether these numbers are even the right order of magnitude; see
    // the report's "Decision" section for what came back.
    'http_req_duration{route:categories}': ['p(95)<250'],
    'http_req_duration{route:search}': ['p(95)<400'],
    'http_req_duration{route:product_detail}': ['p(95)<250'],
    http_req_failed: ['rate<0.01'],
  },
};

function formAction(body) {
  const m = body.match(/<form[^>]*\baction="([^"]+)"/);
  if (!m) throw new Error('no <form action="..."> in response body');
  return m[1].replace(/&amp;/g, '&');
}

// BFF's own session cookie (request_context.cookie_names; "sf_session"
// unless SF_COOKIE_SECURE=true, in which case it's "__Host-sf_session" --
// this script's own setup always runs with cookie_secure=false, since
// there's no TLS terminator in front of this ad-hoc BFF process).
const SESSION_COOKIE = 'sf_session';

function extractCookie(res, name) {
  // `res.cookies` (k6's own parsed-per-response map), not the raw
  // Set-Cookie header: this is a property of one response object, so it
  // survives the jar reset `login()`'s own comment explains.
  const entries = res.cookies[name];
  return entries && entries.length > 0 ? entries[0].value : null;
}

function login(user, clientIp) {
  // The BFF's own auth rate-limit class (LLD-09 §7.2, 20/min) is keyed by
  // source IP, not user -- every k6 VU otherwise shares one real IP
  // (127.0.0.1, no reverse proxy in front here), landing every VU's own
  // one-time login in the same bucket and tripping 429s almost
  // immediately (confirmed directly: the first run of this scenario at
  // just 1 iteration/s got there inside 20s). The system tests hit this
  // same thing first (tests/system/test_bff_login.py) and fixed it the
  // same way: a distinct synthetic IP per simulated user is a closer
  // simulation of real distinct users than this test's own real IP ever
  // could be.
  const headers = { 'X-Forwarded-For': clientIp };
  let res = http.get(`${BASE}/auth/login`, { redirects: 0, headers });
  if (res.status !== 302) throw new Error(`/auth/login: expected 302, got ${res.status}`);
  res = http.get(res.headers['Location'], { redirects: 0 });
  const action = formAction(res.body);
  res = http.post(
    action,
    { username: user.username, password: user.password },
    { redirects: 0 }
  );
  if (res.status !== 302) throw new Error(`Keycloak login form: expected 302, got ${res.status}`);
  res = http.get(res.headers['Location'], { redirects: 0, headers }); // BFF /auth/callback
  check(res, { 'login callback redirects to the app': (r) => r.status === 302 });

  // k6 resets each VU's *implicit* cookie jar at the start of every
  // iteration (by design, so independent iterations don't leak browser
  // state into each other) -- confirmed directly: the first version of
  // this script relied on that jar across iterations and every call
  // after a VU's first iteration came back 401, silently, because
  // `loggedInVUs` (a plain module-scoped object, NOT reset per iteration)
  // said login was already done and skipped it, while the cookie that
  // login produced was already gone. The fix is to never depend on the
  // implicit jar past this function: capture the session cookie's value
  // here and attach it by hand on every later request (see `authHeaders`).
  const sessionValue = extractCookie(res, SESSION_COOKIE);
  if (!sessionValue) throw new Error('BFF callback set no session cookie');
  return sessionValue;
}

function authHeaders(sessionValue) {
  return { Cookie: `${SESSION_COOKIE}=${sessionValue}` };
}

// 3-8s lognormal-ish (LLD-14 §6.1's own "think time 3-8s (lognormal)").
function thinkTime() {
  const u1 = Math.random() || 1e-9;
  const u2 = Math.random();
  const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
  const seconds = Math.exp(Math.log(5) + 0.25 * z); // median 5s
  return Math.min(8, Math.max(3, seconds));
}

// Logged in once per VU, not per iteration -- see the module docstring.
// Keyed by __VU, same as `loggedInVUs` used to be; now holds the session
// cookie's value too, since that's what actually has to survive.
const sessions = {};

export default function () {
  const user = config.users[(__VU - 1) % config.users.length];
  if (!sessions[__VU]) {
    const clientIp = `10.${(__VU >> 8) & 0xff}.${__VU & 0xff}.1`;
    sessions[__VU] = login(user, clientIp);
  }
  const headers = authHeaders(sessions[__VU]);

  let res = http.get(`${BASE}/api/catalog/v1/stores/${config.org_id}/categories`, {
    headers,
    tags: { route: 'categories' },
  });
  check(res, { 'categories 200': (r) => r.status === 200 });
  sleep(thinkTime());

  res = http.get(`${BASE}/api/search/v1/stores/${config.org_id}/search`, {
    headers,
    tags: { route: 'search' },
  });
  check(res, { 'search 200': (r) => r.status === 200 });
  sleep(thinkTime());

  const detailCount = 2 + Math.floor(Math.random() * 3); // 2-4
  for (let i = 0; i < detailCount; i++) {
    const slug = config.slugs[Math.floor(Math.random() * config.slugs.length)];
    res = http.get(`${BASE}/api/catalog/v1/stores/${config.org_id}/products/${slug}`, {
      headers,
      tags: { route: 'product_detail' },
    });
    check(res, { 'product detail 200': (r) => r.status === 200 });
    sleep(thinkTime());
  }
}
