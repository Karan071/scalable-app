import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useParams } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { StatusDot } from '@/components/StatusDot'
import { Button } from '@/components/ui/button'
import { ordersApi, type Order } from '@/lib/api/orders'
import { formatMoney } from '@/lib/money'

/** orders-svc's own admin transition map (LLD-06 §6) -- a linear
 * confirmed -> processing -> shipped -> delivered pipeline. */
const NEXT_STATUS: Record<string, string> = {
  confirmed: 'processing',
  processing: 'shipped',
  shipped: 'delivered',
}

export function OrdersPage() {
  const { orgId } = useParams() as { orgId: string }
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState('')
  const [selected, setSelected] = useState<Order | null>(null)
  const queryKey = ['org', orgId, 'orders', statusFilter]

  const orders = useQuery({
    queryKey,
    queryFn: () => ordersApi.listOrgOrders(orgId, statusFilter || undefined),
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey })

  const transition = useMutation({
    mutationFn: (order: Order) =>
      ordersApi.transitionOrder(orgId, order.id, order.version, NEXT_STATUS[order.status]),
    onSuccess: () => {
      setSelected(null)
      invalidate()
    },
  })
  const cancelOrder = useMutation({
    mutationFn: (order: Order) =>
      ordersApi.cancelOrgOrder(orgId, order.id, order.version, crypto.randomUUID()),
    onSuccess: () => {
      setSelected(null)
      invalidate()
    },
  })
  const refund = useMutation({
    mutationFn: (order: Order) => ordersApi.refundOrgOrder(orgId, order.id, crypto.randomUUID()),
    onSuccess: () => {
      setSelected(null)
      invalidate()
    },
  })

  return (
    <div>
      <PageHeader title="Orders" />
      <ErrorBanner error={orders.error ?? transition.error ?? cancelOrder.error ?? refund.error} />

      <select
        value={statusFilter}
        onChange={(e) => setStatusFilter(e.target.value)}
        className="mb-4 rounded-md border border-border bg-background px-2 py-1.5 text-sm"
      >
        <option value="">All statuses</option>
        {['pending', 'confirmed', 'processing', 'shipped', 'delivered', 'cancelled', 'refunded'].map(
          (s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ),
        )}
      </select>

      {orders.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading...</p>
      ) : orders.data?.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No orders found.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th className="py-2 font-normal">Order</th>
              <th className="py-2 font-normal">Status</th>
              <th className="py-2 font-normal">Payment</th>
              <th className="py-2 font-normal">Total</th>
              <th className="py-2 font-normal" />
            </tr>
          </thead>
          <tbody>
            {orders.data?.items.map((order) => (
              <tr key={order.id} className="border-b border-border/60">
                <td className="py-2 font-mono text-xs">{order.id}</td>
                <td className="py-2">
                  <StatusDot status={order.status} />
                </td>
                <td className="py-2">{order.payment_status}</td>
                <td className="py-2">{formatMoney(order.total_minor, order.currency)}</td>
                <td className="py-2 text-right">
                  <button
                    className="text-sm text-muted-foreground hover:text-foreground hover:underline"
                    onClick={() => setSelected(order)}
                  >
                    Manage
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selected && (
        <div className="mt-6 flex max-w-md flex-col gap-3 rounded-md border border-border p-4">
          <div className="text-sm font-medium">Order {selected.id}</div>
          <div className="flex gap-2">
            {NEXT_STATUS[selected.status] && (
              <Button disabled={transition.isPending} onClick={() => transition.mutate(selected)}>
                Mark {NEXT_STATUS[selected.status]}
              </Button>
            )}
            {(selected.status === 'confirmed' || selected.status === 'processing') && (
              <Button
                variant="destructive"
                disabled={cancelOrder.isPending}
                onClick={() => {
                  if (window.confirm('Cancel this order?')) cancelOrder.mutate(selected)
                }}
              >
                Cancel
              </Button>
            )}
            {selected.status === 'delivered' && (
              <Button
                variant="outline"
                disabled={refund.isPending}
                onClick={() => {
                  if (window.confirm('Refund this order?')) refund.mutate(selected)
                }}
              >
                Refund
              </Button>
            )}
            <Button variant="ghost" onClick={() => setSelected(null)}>
              Close
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
