import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useParams } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { Button } from '@/components/ui/button'
import { inventoryApi, type StockItem } from '@/lib/api/inventory'

export function InventoryPage() {
  const { orgId } = useParams() as { orgId: string }
  const queryClient = useQueryClient()
  const [lowStockOnly, setLowStockOnly] = useState(false)
  const [adjusting, setAdjusting] = useState<StockItem | null>(null)
  const [delta, setDelta] = useState('')
  const [reason, setReason] = useState('')
  const queryKey = ['org', orgId, 'stock', lowStockOnly]

  const stock = useQuery({ queryKey, queryFn: () => inventoryApi.listStock(orgId, lowStockOnly) })

  const adjust = useMutation({
    mutationFn: () => {
      if (!adjusting) throw new Error('nothing selected')
      return inventoryApi.adjustStock(orgId, adjusting.sku_id, adjusting.version, {
        adjustment_id: crypto.randomUUID(),
        delta: Number(delta),
        reason,
      })
    },
    onSuccess: () => {
      setAdjusting(null)
      setDelta('')
      setReason('')
      queryClient.invalidateQueries({ queryKey })
    },
  })

  return (
    <div>
      <PageHeader title="Inventory" />
      <ErrorBanner error={stock.error ?? adjust.error} />

      <label className="mb-4 flex items-center gap-2 text-sm text-muted-foreground">
        <input
          type="checkbox"
          checked={lowStockOnly}
          onChange={(e) => setLowStockOnly(e.target.checked)}
        />
        Low stock only
      </label>

      {stock.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading...</p>
      ) : stock.data?.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No stock items.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th className="py-2 font-normal">SKU</th>
              <th className="py-2 font-normal">On hand</th>
              <th className="py-2 font-normal">Reserved</th>
              <th className="py-2 font-normal">Available</th>
              <th className="py-2 font-normal" />
            </tr>
          </thead>
          <tbody>
            {stock.data?.items.map((item) => (
              <tr key={item.sku_id} className="border-b border-border/60">
                <td className="py-2 font-mono text-xs">{item.sku_id}</td>
                <td className="py-2">{item.on_hand}</td>
                <td className="py-2">{item.reserved}</td>
                <td className="py-2">{item.available}</td>
                <td className="py-2 text-right">
                  <button
                    className="text-sm text-muted-foreground hover:text-foreground hover:underline"
                    onClick={() => setAdjusting(item)}
                  >
                    Adjust
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {adjusting && (
        <form
          className="mt-6 flex max-w-md flex-col gap-3 rounded-md border border-border p-4"
          onSubmit={(e) => {
            e.preventDefault()
            if (delta && reason) adjust.mutate()
          }}
        >
          <div className="text-sm font-medium">Adjust {adjusting.sku_id}</div>
          <div>
            <label htmlFor="adjust-delta" className="mb-1 block text-xs text-muted-foreground">
              Delta (+/-)
            </label>
            <input
              id="adjust-delta"
              required
              type="number"
              value={delta}
              onChange={(e) => setDelta(e.target.value)}
              className="w-28 rounded-md border border-border bg-background px-2.5 py-1.5 text-sm"
            />
          </div>
          <div>
            <label htmlFor="adjust-reason" className="mb-1 block text-xs text-muted-foreground">
              Reason
            </label>
            <input
              id="adjust-reason"
              required
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm"
            />
          </div>
          <div className="flex gap-2">
            <Button type="submit" disabled={adjust.isPending}>
              Apply
            </Button>
            <Button type="button" variant="ghost" onClick={() => setAdjusting(null)}>
              Cancel
            </Button>
          </div>
        </form>
      )}
    </div>
  )
}
