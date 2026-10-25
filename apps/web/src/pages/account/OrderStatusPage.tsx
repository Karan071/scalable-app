import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { StatusDot } from '@/components/StatusDot'
import { Button } from '@/components/ui/button'
import { ORDER_IN_PROGRESS_STATUSES, ordersApi } from '@/lib/api/orders'
import { formatMoney } from '@/lib/money'

const POLL_INTERVAL_MS = 2000

export function OrderStatusPage() {
  const { orderId } = useParams() as { orderId: string }
  const queryClient = useQueryClient()
  const queryKey = ['me', 'orders', orderId]

  const order = useQuery({
    queryKey,
    queryFn: () => ordersApi.getMyOrder(orderId),
    // LLD-15 §7's SSE is M4 scope; this polls instead while the saga is
    // still running, same fallback cadence the long-poll route would use.
    refetchInterval: (query) =>
      query.state.data && ORDER_IN_PROGRESS_STATUSES.has(query.state.data.status)
        ? POLL_INTERVAL_MS
        : false,
  })

  const cancel = useMutation({
    mutationFn: () => ordersApi.cancelMyOrder(orderId, crypto.randomUUID()),
    onSuccess: () => queryClient.invalidateQueries({ queryKey }),
  })

  if (order.isLoading) return <p className="text-sm text-muted-foreground">Loading...</p>
  if (order.error) return <ErrorBanner error={order.error} />
  if (!order.data) return null

  const data = order.data

  return (
    <div className="max-w-xl">
      <PageHeader title={`Order ${data.id}`} />
      <ErrorBanner error={cancel.error} />

      <div className="mb-4 flex items-center gap-3">
        <StatusDot status={data.status} />
        <span className="text-sm text-muted-foreground">payment: {data.payment_status}</span>
      </div>

      {data.status === 'pending' && (
        <p className="mb-4 text-sm text-muted-foreground">
          We're confirming your order -- this updates automatically.
        </p>
      )}

      <table className="mb-4 w-full text-sm">
        <tbody>
          {data.lines.map((line) => (
            <tr key={line.sku_id} className="border-b border-border/60">
              <td className="py-2">{line.name_snapshot}</td>
              <td className="py-2 text-right">x{line.qty}</td>
              <td className="py-2 text-right">{formatMoney(line.line_total_minor, data.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="mb-4 flex items-center justify-between border-t border-border pt-3 text-sm font-medium">
        <span>Total</span>
        <span>{formatMoney(data.total_minor, data.currency)}</span>
      </div>

      {data.status === 'confirmed' && (
        <Button
          variant="destructive"
          disabled={cancel.isPending}
          onClick={() => {
            if (window.confirm('Cancel this order?')) cancel.mutate()
          }}
        >
          Cancel order
        </Button>
      )}
    </div>
  )
}
