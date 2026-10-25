import { apiClient } from '../api-client'

export interface AvailabilityItem {
  sku_id: string
  in_stock: boolean
  low_stock: boolean
}

export interface StockItem {
  sku_id: string
  product_id: string
  on_hand: number
  reserved: number
  available: number
  low_stock_threshold: number
  version: number
}

export interface StockListResponse {
  items: StockItem[]
  next_cursor: string | null
}

export const inventoryApi = {
  // --- storefront (public) ---------------------------------------------------
  getAvailability: (orgId: string, skuIds: string[]) => {
    if (skuIds.length === 0) return Promise.resolve({ items: [] as AvailabilityItem[] })
    const params = new URLSearchParams()
    for (const id of skuIds) params.append('sku_ids', id)
    return apiClient.get<{ items: AvailabilityItem[] }>(
      `/api/inventory/v1/stores/${orgId}/availability?${params.toString()}`,
    )
  },

  // --- commerce admin (console) -----------------------------------------------
  listStock: (orgId: string, lowStockOnly?: boolean) => {
    const params = new URLSearchParams()
    if (lowStockOnly) params.set('low_stock', 'true')
    return apiClient.get<StockListResponse>(
      `/api/inventory/v1/orgs/${orgId}/stock?${params.toString()}`,
    )
  },

  adjustStock: (
    orgId: string,
    skuId: string,
    version: number,
    body: { adjustment_id: string; delta: number; reason: string },
  ) =>
    apiClient.post<StockItem>(
      `/api/inventory/v1/orgs/${orgId}/stock/${skuId}:adjust`,
      body,
      { 'if-match': `"v${version}"` },
    ),
}
