import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useParams } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { Button } from '@/components/ui/button'
import { catalogApi } from '@/lib/api/catalog'
import { formatMoney } from '@/lib/money'

export function ProductAdminPage() {
  const { orgId, productId } = useParams() as { orgId: string; productId: string }
  const queryClient = useQueryClient()
  const queryKey = ['org', orgId, 'products', productId]
  const [skuCode, setSkuCode] = useState('')
  const [priceMinor, setPriceMinor] = useState('')

  const product = useQuery({ queryKey, queryFn: () => catalogApi.getProduct(orgId, productId) })

  const addSku = useMutation({
    mutationFn: () =>
      catalogApi.putSku(orgId, productId, crypto.randomUUID(), {
        sku_code: skuCode,
        price_minor: Math.round(Number(priceMinor) * 100),
      }),
    onSuccess: () => {
      setSkuCode('')
      setPriceMinor('')
      queryClient.invalidateQueries({ queryKey })
    },
  })

  if (product.isLoading) return <p className="text-sm text-muted-foreground">Loading...</p>
  if (product.error) return <ErrorBanner error={product.error} />
  if (!product.data) return null

  return (
    <div className="max-w-xl">
      <PageHeader title={product.data.name} />
      <ErrorBanner error={addSku.error} />

      <table className="mb-6 w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-muted-foreground">
            <th className="py-2 font-normal">SKU code</th>
            <th className="py-2 font-normal">Price</th>
            <th className="py-2 font-normal">Status</th>
          </tr>
        </thead>
        <tbody>
          {product.data.skus.map((sku) => (
            <tr key={sku.id} className="border-b border-border/60">
              <td className="py-2 font-mono text-xs">{sku.sku_code}</td>
              <td className="py-2">{formatMoney(sku.price_minor)}</td>
              <td className="py-2">{sku.status}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <form
        className="flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (skuCode && priceMinor) addSku.mutate()
        }}
      >
        <div>
          <label htmlFor="sku-code" className="mb-1 block text-xs text-muted-foreground">
            SKU code
          </label>
          <input
            id="sku-code"
            required
            value={skuCode}
            onChange={(e) => setSkuCode(e.target.value)}
            className="rounded-md border border-border bg-background px-2.5 py-1.5 text-sm"
          />
        </div>
        <div>
          <label htmlFor="sku-price" className="mb-1 block text-xs text-muted-foreground">
            Price (USD)
          </label>
          <input
            id="sku-price"
            required
            type="number"
            min="0"
            step="0.01"
            value={priceMinor}
            onChange={(e) => setPriceMinor(e.target.value)}
            className="w-28 rounded-md border border-border bg-background px-2.5 py-1.5 text-sm"
          />
        </div>
        <Button type="submit" disabled={addSku.isPending}>
          Add SKU
        </Button>
      </form>
    </div>
  )
}
