import { apiClient } from '../api-client'

export interface SearchHit {
  product_id: string
  slug: string
  name: string
  primary_image_key: string | null
  min_price_minor: number | null
  max_price_minor: number | null
  in_stock: boolean
}

export interface SearchResponse {
  items: SearchHit[]
  next_cursor: string | null
}

export interface SearchParams {
  q?: string
  category?: string
  priceMin?: number
  priceMax?: number
  inStock?: boolean
  sort?: string
  cursor?: string
}

export const searchApi = {
  search: (orgId: string, params: SearchParams, signal?: AbortSignal) => {
    const qs = new URLSearchParams()
    if (params.q) qs.set('q', params.q)
    if (params.category) qs.set('category', params.category)
    if (params.priceMin !== undefined) qs.set('price_min', String(params.priceMin))
    if (params.priceMax !== undefined) qs.set('price_max', String(params.priceMax))
    if (params.inStock !== undefined) qs.set('in_stock', String(params.inStock))
    if (params.sort) qs.set('sort', params.sort)
    if (params.cursor) qs.set('cursor', params.cursor)
    return apiClient.get<SearchResponse>(
      `/api/search/v1/stores/${orgId}/search?${qs.toString()}`,
      signal,
    )
  },
}
