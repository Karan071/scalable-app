import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'

import { ApiError, apiClient, setCsrfToken } from '../api-client'
import { server } from '../../test/msw-server'

afterEach(() => setCsrfToken(null))

describe('apiClient', () => {
  it('attaches X-CSRF-Token on unsafe methods', async () => {
    setCsrfToken('the-csrf-token')
    let seenHeader: string | null = null
    server.use(
      http.post('/api/orgs/v1/orgs/:id/members/:mid\\:disable', ({ request }) => {
        seenHeader = request.headers.get('x-csrf-token')
        return HttpResponse.json({ status: 'disabled' })
      }),
    )

    await apiClient.post('/api/orgs/v1/orgs/org-1/members/mem-1:disable')

    expect(seenHeader).toBe('the-csrf-token')
  })

  it('does not attach X-CSRF-Token on GET', async () => {
    let seenHeader: string | null | undefined
    server.use(
      http.get('/api/orgs/v1/me/memberships', ({ request }) => {
        seenHeader = request.headers.get('x-csrf-token')
        return HttpResponse.json([])
      }),
    )
    setCsrfToken('should-not-be-sent')

    await apiClient.get('/api/orgs/v1/me/memberships')

    expect(seenHeader).toBeNull()
  })

  it('parses an RFC 9457 problem into a typed ApiError', async () => {
    server.use(
      http.get('/api/orgs/v1/orgs/org-1/members', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'forbidden', status: 403, detail: 'no access' },
          { status: 403 },
        ),
      ),
    )

    await expect(apiClient.get('/api/orgs/v1/orgs/org-1/members')).rejects.toSatisfy((err) => {
      expect(err).toBeInstanceOf(ApiError)
      expect((err as ApiError).problem.status).toBe(403)
      expect((err as ApiError).problem.title).toBe('forbidden')
      expect((err as ApiError).problem.detail).toBe('no access')
      return true
    })
  })

  it('retries once on a csrf-invalid 403 after refreshing the token', async () => {
    let postAttempts = 0
    server.use(
      http.get('/auth/session', () =>
        HttpResponse.json({ authenticated: true, csrf_token: 'fresh-token' }),
      ),
      http.post('/api/orgs/v1/orgs/:id/invitations', ({ request }) => {
        postAttempts += 1
        const header = request.headers.get('x-csrf-token')
        if (header !== 'fresh-token') {
          return HttpResponse.json(
            { type: 'about:blank', title: 'csrf-invalid', status: 403 },
            { status: 403 },
          )
        }
        return HttpResponse.json({ invitation_id: 'inv-1', expires_at: '2026-01-01T00:00:00Z' })
      }),
    )
    setCsrfToken('stale-token')

    const result = await apiClient.post('/api/orgs/v1/orgs/org-1/invitations', {
      email: 'a@example.com',
      roles_requested: ['org_admin'],
    })

    expect(postAttempts).toBe(2)
    expect(result).toEqual({ invitation_id: 'inv-1', expires_at: '2026-01-01T00:00:00Z' })
  })

  it('surfaces a step_up_required 401 as an ApiError carrying step_up_url', async () => {
    server.use(
      http.get('/api/users/v1/platform/users', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'step_up_required', status: 401, step_up_url: '/auth/step-up' },
          { status: 401 },
        ),
      ),
    )

    await expect(apiClient.get('/api/users/v1/platform/users')).rejects.toSatisfy((err) => {
      expect(err).toBeInstanceOf(ApiError)
      expect((err as ApiError).problem.step_up_url).toBe('/auth/step-up')
      return true
    })
  })

  it('retries a GET once after a 429 honoring Retry-After, then succeeds', async () => {
    let attempts = 0
    server.use(
      http.get('/api/orgs/v1/orgs/org-1/members', () => {
        attempts += 1
        if (attempts === 1) {
          return new HttpResponse(
            JSON.stringify({ type: 'about:blank', title: 'rate-limited', status: 429 }),
            { status: 429, headers: { 'retry-after': '0', 'content-type': 'application/json' } },
          )
        }
        return HttpResponse.json([])
      }),
    )

    const result = await apiClient.get('/api/orgs/v1/orgs/org-1/members')

    expect(attempts).toBe(2)
    expect(result).toEqual([])
  })
})
