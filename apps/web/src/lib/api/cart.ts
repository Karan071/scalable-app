import { apiClient } from '../api-client'

export interface CartItem {
  sku_id: string
  product_id: string
  qty: number
}

export interface Cart {
  id: string
  org_id: string
  status: string
  version: number
  items: CartItem[]
}

export const cartApi = {
  getCart: (orgId: string) => apiClient.get<Cart>(`/api/cart/v1/me/carts/${orgId}`),

  setItem: (orgId: string, skuId: string, qty: number) =>
    apiClient.put<Cart>(`/api/cart/v1/me/carts/${orgId}/items/${skuId}`, { qty }),

  removeItem: (orgId: string, skuId: string) =>
    apiClient.delete<Cart>(`/api/cart/v1/me/carts/${orgId}/items/${skuId}`),

  clearItems: (orgId: string) => apiClient.delete<Cart>(`/api/cart/v1/me/carts/${orgId}/items`),
}
