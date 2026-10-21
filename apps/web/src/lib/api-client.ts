/**
 * Thin fetch wrapper over the BFF's /api/** proxy (LLD-15 §4).
 *
 * - `credentials: 'same-origin'` -- the session cookie is first-party.
 * - JSON only.
 * - Parses RFC 9457 problem+json into a typed `ApiError`.
 * - Attaches `X-CSRF-Token` on unsafe methods from an in-memory token (never
 *   persisted -- LLD-15 §3's storage rule).
 * - One retry on a `csrf-invalid`-shaped 403: re-fetches /auth/session for a
 *   fresh token, then retries once.
 * - `Retry-After` is honored for 429/503 on GET only (idempotent requests).
 */

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

export interface ApiProblem {
  type: string
  title: string
  status: number
  detail?: string
  instance?: string
  errors?: unknown
  step_up_url?: string
}

export class ApiError extends Error {
  readonly problem: ApiProblem

  constructor(problem: ApiProblem) {
    super(problem.title)
    this.problem = problem
    this.name = 'ApiError'
  }
}

let csrfToken: string | null = null

export function setCsrfToken(token: string | null): void {
  csrfToken = token
}

export function getCsrfToken(): string | null {
  return csrfToken
}

async function refreshCsrfToken(): Promise<void> {
  const resp = await fetch('/auth/session', { credentials: 'same-origin' })
  if (!resp.ok) return
  const body = (await resp.json()) as { csrf_token?: string }
  if (body.csrf_token) csrfToken = body.csrf_token
}

async function parseProblem(resp: Response): Promise<ApiProblem> {
  try {
    const body = (await resp.json()) as Partial<ApiProblem>
    return {
      type: body.type ?? 'about:blank',
      title: body.title ?? resp.statusText,
      status: resp.status,
      detail: body.detail,
      instance: body.instance,
      errors: body.errors,
      step_up_url: body.step_up_url,
    }
  } catch {
    return { type: 'about:blank', title: resp.statusText, status: resp.status }
  }
}

function isCsrfInvalid(problem: ApiProblem): boolean {
  return problem.status === 403 && problem.title === 'csrf-invalid'
}

export interface RequestOptions {
  method?: string
  body?: unknown
  signal?: AbortSignal
  headers?: Record<string, string>
}

async function doFetch(path: string, options: RequestOptions): Promise<Response> {
  const method = options.method ?? 'GET'
  const headers: Record<string, string> = { ...options.headers }

  if (options.body !== undefined) {
    headers['content-type'] = 'application/json'
  }
  if (UNSAFE_METHODS.has(method) && csrfToken) {
    headers['x-csrf-token'] = csrfToken
  }

  return fetch(path, {
    method,
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    credentials: 'same-origin',
    signal: options.signal,
  })
}

/** Sleeps honoring a `Retry-After` header (seconds), capped to avoid a hung UI. */
async function waitForRetryAfter(resp: Response): Promise<void> {
  const raw = resp.headers.get('retry-after')
  const seconds = raw ? Math.min(Number(raw) || 1, 30) : 1
  await new Promise((resolve) => setTimeout(resolve, seconds * 1000))
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET'
  let resp = await doFetch(path, options)

  if (!resp.ok) {
    let problem = await parseProblem(resp)

    if (isCsrfInvalid(problem) && UNSAFE_METHODS.has(method)) {
      await refreshCsrfToken()
      resp = await doFetch(path, options)
      if (!resp.ok) problem = await parseProblem(resp)
      else return (await resp.json()) as T
    }

    if (
      (problem.status === 429 || problem.status === 503) &&
      method === 'GET' &&
      !resp.ok
    ) {
      await waitForRetryAfter(resp)
      resp = await doFetch(path, options)
      if (resp.ok) return (await resp.json()) as T
      problem = await parseProblem(resp)
    }

    throw new ApiError(problem)
  }

  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}

export const apiClient = {
  get: <T>(path: string, signal?: AbortSignal) => apiFetch<T>(path, { signal }),
  post: <T>(path: string, body?: unknown) => apiFetch<T>(path, { method: 'POST', body }),
  patch: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    apiFetch<T>(path, { method: 'PATCH', body, headers }),
  delete: <T>(path: string, headers?: Record<string, string>) =>
    apiFetch<T>(path, { method: 'DELETE', headers }),
}
