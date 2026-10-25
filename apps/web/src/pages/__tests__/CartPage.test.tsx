import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { CartPage } from '../storefront/CartPage'
import { server } from '../../test/msw-server'

function renderAtOrg(orgId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/s/${orgId}/cart`]}>
        <Routes>
          <Route path="/s/:orgId/cart" element={<CartPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('CartPage', () => {
  it('enriches cart lines with name/price from catalog-svc and shows a subtotal', async () => {
    server.use(
      http.get('/api/cart/v1/me/carts/org-1', () =>
        HttpResponse.json({
          id: 'cart-1',
          org_id: 'org-1',
          status: 'active',
          version: 1,
          items: [{ sku_id: 'sku-1', product_id: 'prod-1', qty: 2 }],
        }),
      ),
      http.get('/api/catalog/v1/stores/org-1/skus', () =>
        HttpResponse.json({
          items: [
            { sku_id: 'sku-1', product_id: 'prod-1', product_name: 'Mug', price_minor: 500 },
          ],
        }),
      ),
    )

    renderAtOrg('org-1')

    await waitFor(() => expect(screen.getByText('Mug')).toBeInTheDocument())
    // 2 x $5.00 line total, and the (identical, single-line) subtotal.
    expect(screen.getAllByText('$10.00')).toHaveLength(2)
    expect(screen.getByText('Subtotal')).toBeInTheDocument()
  })

  it('shows an empty state when the cart has no items', async () => {
    server.use(
      http.get('/api/cart/v1/me/carts/org-1', () =>
        HttpResponse.json({ id: 'cart-1', org_id: 'org-1', status: 'active', version: 1, items: [] }),
      ),
    )

    renderAtOrg('org-1')

    await waitFor(() => expect(screen.getByText('Your cart is empty.')).toBeInTheDocument())
  })
})
