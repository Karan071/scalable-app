import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { MembersPage } from '../console/MembersPage'
import { server } from '../../test/msw-server'

function renderAtOrg(orgId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/console/orgs/${orgId}/members`]}>
        <Routes>
          <Route path="/console/orgs/:orgId/members" element={<MembersPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('MembersPage', () => {
  it('renders members from /api/orgs/v1/orgs/:id/members', async () => {
    server.use(
      http.get('/api/orgs/v1/orgs/org-1/members', () =>
        HttpResponse.json([
          {
            id: 'mem-1',
            org_id: 'org-1',
            user_id: 'user-1',
            status: 'active',
            version: 1,
            org_name: null,
            org_slug: null,
          },
        ]),
      ),
    )

    renderAtOrg('org-1')

    await waitFor(() => expect(screen.getByText('user-1')).toBeInTheDocument())
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.getByText('Disable')).toBeInTheDocument()
  })

  it('shows an empty state when there are no members', async () => {
    server.use(
      http.get('/api/orgs/v1/orgs/org-1/members', () => HttpResponse.json([])),
    )

    renderAtOrg('org-1')

    await waitFor(() => expect(screen.getByText('No members yet.')).toBeInTheDocument())
  })

  it('shows an error banner on a 403 forbidden response', async () => {
    server.use(
      http.get('/api/orgs/v1/orgs/org-1/members', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'forbidden', status: 403 },
          { status: 403 },
        ),
      ),
    )

    renderAtOrg('org-1')

    await waitFor(() =>
      expect(screen.getByText("You don't have access to do that.")).toBeInTheDocument(),
    )
  })
})
