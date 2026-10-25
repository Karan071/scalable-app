import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { StatusDot } from '@/components/StatusDot'
import { ordersApi } from '@/lib/api/orders'
import { formatMoney } from '@/lib/money'

export function OrderHistoryPage() {
  const orders = useQuery({ queryKey: ['me', 'orders'], queryFn: () => ordersApi.listMyOrders() })

  return (
    <div>
      <PageHeader title="Orders" />
      <ErrorBanner error={orders.error} />

      {orders.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading...</p>
      ) : orders.data?.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No orders yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th className="py-2 font-normal">Order</th>
              <th className="py-2 font-normal">Status</th>
              <th className="py-2 font-normal">Total</th>
            </tr>
          </thead>
          <tbody>
            {orders.data?.items.map((order) => (
              <tr key={order.id} className="border-b border-border/60">
                <td className="py-2">
                  <Link to={`/account/orders/${order.id}`} className="font-mono text-xs hover:underline">
                    {order.id}
                  </Link>
                </td>
                <td className="py-2">
                  <StatusDot status={order.status} />
                </td>
                <td className="py-2">{formatMoney(order.total_minor, order.currency)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
