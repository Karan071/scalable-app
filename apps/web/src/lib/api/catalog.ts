import { apiClient } from '../api-client'

export interface Sku {
  id: string
  sku_code: string
  attributes: Record<string, unknown>
  price_minor: number
  compare_at_minor: number | null
  status: string
  version: number
}

export interface Product {
  id: string
  org_id: string
  slug: string
  name: string
  description_md: string
  description_html: string
  status: string
  category_id: string | null
  version: number
  skus: Sku[]
}

export interface ProductPage {
  items: Product[]
  next_cursor: string | null
}

export interface Category {
  id: string
  org_id: string
  parent_id: string | null
  path: string
  name: string
  slug: string
}

export interface StorefrontSku {
  sku_id: string
  product_id: string
  product_name: string
  price_minor: number
}

export const catalogApi = {
  // --- storefront (public) reads --------------------------------------------
  getStorefrontProduct: (orgId: string, slug: string) =>
    apiClient.get<Product>(`/api/catalog/v1/stores/${orgId}/products/${slug}`),

  listStorefrontCategories: (orgId: string) =>
    apiClient.get<Category[]>(`/api/catalog/v1/stores/${orgId}/categories`),

  listStorefrontSkus: (orgId: string, skuIds: string[]) => {
    const params = new URLSearchParams()
    for (const id of skuIds) params.append('sku_ids', id)
    return apiClient.get<{ items: StorefrontSku[] }>(
      `/api/catalog/v1/stores/${orgId}/skus?${params.toString()}`,
    )
  },

  // --- commerce admin (console) ---------------------------------------------
  listProducts: (orgId: string, cursor?: string) => {
    const params = new URLSearchParams()
    if (cursor) params.set('cursor', cursor)
    return apiClient.get<ProductPage>(`/api/catalog/v1/orgs/${orgId}/products?${params.toString()}`)
  },

  getProduct: (orgId: string, productId: string) =>
    apiClient.get<Product>(`/api/catalog/v1/orgs/${orgId}/products/${productId}`),

  createProduct: (orgId: string, body: { slug: string; name: string; description_md?: string }) =>
    apiClient.post<Product>(`/api/catalog/v1/orgs/${orgId}/products`, body),

  publishProduct: (orgId: string, productId: string, version: number) =>
    apiClient.post<Product>(
      `/api/catalog/v1/orgs/${orgId}/products/${productId}:publish`,
      {},
      { 'if-match': `"v${version}"` },
    ),

  archiveProduct: (orgId: string, productId: string, version: number) =>
    apiClient.post<Product>(
      `/api/catalog/v1/orgs/${orgId}/products/${productId}:archive`,
      {},
      { 'if-match': `"v${version}"` },
    ),

  putSku: (
    orgId: string,
    productId: string,
    skuId: string,
    body: { sku_code: string; price_minor: number; attributes?: Record<string, unknown> },
  ) => apiClient.put<Sku>(`/api/catalog/v1/orgs/${orgId}/products/${productId}/skus/${skuId}`, body),
}
