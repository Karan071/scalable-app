import { apiClient } from '../api-client'

export interface ShippingAddress {
  name: string
  line1: string
  line2?: string | null
  city: string
  postal_code: string
  country: string
}

export interface OrderLine {
  sku_id: string
  product_id: string
  name_snapshot: string
  unit_price_minor: number
  qty: number
  line_total_minor: number
}

export interface OrderStatusHistoryEntry {
  from_status: string | null
  to_status: string
  actor_type: string
  reason: string | null
  at: string
}

export interface Order {
  id: string
  org_id: string
  status: string
  payment_status: string
  currency: string
  subtotal_minor: number
  discount_minor: number
  shipping_minor: number
  tax_minor: number
  total_minor: number
  promo_code: string | null
  cancel_reason: string | null
  version: number
  created_at: string
  updated_at: string
}

export interface OrderDetail extends Order {
  lines: OrderLine[]
  history: OrderStatusHistoryEntry[]
}

export interface OrderListResponse {
  items: Order[]
  next_cursor: string | null
}

/** The checkout saga (LLD-06 §5) has many internal states, but `Order.status`
 * itself only ever shows one of `ORDER_STATUSES` -- `pending` the whole time
 * checkout is in flight, then `confirmed`/`cancelled` once the saga
 * resolves. The order status page polls while it's still `pending`. */
export const ORDER_IN_PROGRESS_STATUSES = new Set(['pending'])

export const ordersApi = {
  // --- customer (storefront + account) ---------------------------------------
  checkout: (
    orgId: string,
    idempotencyKey: string,
    body: {
      cart_version: number
      promo_code?: string | null
      shipping_address: ShippingAddress
      payment_method_token: string
    },
  ) =>
    apiClient.post<{ order_id: string; status: string }>(
      `/api/orders/v1/stores/${orgId}/checkouts`,
      body,
      { 'idempotency-key': idempotencyKey },
    ),

  listMyOrders: (cursor?: string) => {
    const params = new URLSearchParams()
    if (cursor) params.set('cursor', cursor)
    return apiClient.get<OrderListResponse>(`/api/orders/v1/me/orders?${params.toString()}`)
  },

  getMyOrder: (orderId: string) => apiClient.get<OrderDetail>(`/api/orders/v1/me/orders/${orderId}`),

  cancelMyOrder: (orderId: string, idempotencyKey: string) =>
    apiClient.post<{ order_id: string; status: string }>(
      `/api/orders/v1/me/orders/${orderId}:cancel`,
      {},
      { 'idempotency-key': idempotencyKey },
    ),

  // --- commerce admin (console) -----------------------------------------------
  listOrgOrders: (orgId: string, status?: string, cursor?: string) => {
    const params = new URLSearchParams()
    if (status) params.set('status', status)
    if (cursor) params.set('cursor', cursor)
    return apiClient.get<OrderListResponse>(
      `/api/orders/v1/orgs/${orgId}/orders?${params.toString()}`,
    )
  },

  getOrgOrder: (orgId: string, orderId: string) =>
    apiClient.get<OrderDetail>(`/api/orders/v1/orgs/${orgId}/orders/${orderId}`),

  transitionOrder: (orgId: string, orderId: string, version: number, to: string) =>
    apiClient.post<Order>(
      `/api/orders/v1/orgs/${orgId}/orders/${orderId}:transition`,
      { to },
      { 'if-match': `"v${version}"` },
    ),

  cancelOrgOrder: (orgId: string, orderId: string, version: number, idempotencyKey: string) =>
    apiClient.post<{ order_id: string; status: string }>(
      `/api/orders/v1/orgs/${orgId}/orders/${orderId}:cancel`,
      {},
      { 'idempotency-key': idempotencyKey, 'if-match': `"v${version}"` },
    ),

  refundOrgOrder: (orgId: string, orderId: string, idempotencyKey: string) =>
    apiClient.post<{ order_id: string; payment_status: string }>(
      `/api/orders/v1/orgs/${orgId}/orders/${orderId}:refund`,
      {},
      { 'idempotency-key': idempotencyKey },
    ),
}
