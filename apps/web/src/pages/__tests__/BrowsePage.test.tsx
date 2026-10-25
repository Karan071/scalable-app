import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { BrowsePage } from '../storefront/BrowsePage'
import { server } from '../../test/msw-server'

function renderAtOrg(orgId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/s/${orgId}`]}>
        <Routes>
          <Route path="/s/:orgId" element={<BrowsePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('BrowsePage', () => {
  it('browses with no q param before typing, then searches after a debounced keystroke', async () => {
    const seenQueries: (string | null)[] = []
    server.use(
      http.get('/api/search/v1/stores/org-1/search', ({ request }) => {
        seenQueries.push(new URL(request.url).searchParams.get('q'))
        return HttpResponse.json({ items: [], next_cursor: null })
      }),
    )

    renderAtOrg('org-1')
    await waitFor(() => expect(seenQueries).toContain(null))

    const input = screen.getByPlaceholderText('Search products...')
    fireEvent.change(input, { target: { value: 'm' } })
    fireEvent.change(input, { target: { value: 'mug' } })

    await waitFor(() => expect(seenQueries).toContain('mug'))
    // Only the settled "mug" fires a search request -- the intermediate
    // "m" keystroke never gets its own 300ms window (LLD-15 §5's debounce).
    expect(seenQueries).not.toContain('m')
  })

  it('renders search hits with price and an out-of-stock flag', async () => {
    server.use(
      http.get('/api/search/v1/stores/org-1/search', () =>
        HttpResponse.json({
          items: [
            {
              product_id: 'prod-1',
              slug: 'mug',
              name: 'Mug',
              primary_image_key: null,
              min_price_minor: 500,
              max_price_minor: 500,
              in_stock: false,
            },
          ],
          next_cursor: null,
        }),
      ),
    )

    renderAtOrg('org-1')

    await waitFor(() => expect(screen.getByText('Mug')).toBeInTheDocument())
    expect(screen.getByText('$5.00')).toBeInTheDocument()
    expect(screen.getByText('Out of stock')).toBeInTheDocument()
  })
})
