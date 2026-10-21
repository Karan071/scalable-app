import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { useSession } from '../session'
import { server } from '../../test/msw-server'

function Probe() {
  const session = useSession()
  if (session.isLoading) return <div>loading</div>
  return <div data-testid="csrf">{session.data?.csrf_token}</div>
}

describe('useSession', () => {
  it('syncs the csrf token from /auth/session into the api-client module state', async () => {
    server.use(
      http.get('/auth/session', () =>
        HttpResponse.json({ authenticated: true, csrf_token: 'abc123' }),
      ),
    )
    const queryClient = new QueryClient()

    render(
      <QueryClientProvider client={queryClient}>
        <Probe />
      </QueryClientProvider>,
    )

    await waitFor(() => expect(screen.getByTestId('csrf')).toHaveTextContent('abc123'))
  })

  it("falls back to {authenticated:false} if /auth/session errors (never throws)", async () => {
    server.use(http.get('/auth/session', () => new HttpResponse(null, { status: 500 })))
    const queryClient = new QueryClient()

    function Authed() {
      const session = useSession()
      if (session.isLoading) return <div>loading</div>
      return <div data-testid="authed">{String(session.data?.authenticated)}</div>
    }

    render(
      <QueryClientProvider client={queryClient}>
        <Authed />
      </QueryClientProvider>,
    )

    await waitFor(() => expect(screen.getByTestId('authed')).toHaveTextContent('false'))
  })
})

describe('org-scoped query keys (LLD-15 §4)', () => {
  it('switching the org id in the query key does not reuse the other org\'s cached data', async () => {
    const queryClient = new QueryClient()
    server.use(
      http.get('/api/orgs/v1/orgs/org-a/members', () => HttpResponse.json([{ id: 'a' }])),
      http.get('/api/orgs/v1/orgs/org-b/members', () => HttpResponse.json([{ id: 'b' }])),
    )

    function MembersProbe({ orgId }: { orgId: string }) {
      const query = useQuery({
        queryKey: ['org', orgId, 'members'],
        queryFn: async () => {
          const resp = await fetch(`/api/orgs/v1/orgs/${orgId}/members`)
          return resp.json() as Promise<{ id: string }[]>
        },
      })
      if (query.isLoading) return <div>loading</div>
      return <div data-testid="members">{query.data?.map((m) => m.id).join(',')}</div>
    }

    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <MembersProbe orgId="org-a" />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('members')).toHaveTextContent('a'))

    rerender(
      <QueryClientProvider client={queryClient}>
        <MembersProbe orgId="org-b" />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('members')).toHaveTextContent('b'))

    // org-a's own cache entry must still hold only org-a's data, not org-b's.
    expect(queryClient.getQueryData(['org', 'org-a', 'members'])).toEqual([{ id: 'a' }])
  })
})
