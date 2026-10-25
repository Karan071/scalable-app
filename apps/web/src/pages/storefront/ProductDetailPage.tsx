import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useParams } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { Button } from '@/components/ui/button'
import { cartApi } from '@/lib/api/cart'
import { catalogApi } from '@/lib/api/catalog'
import { inventoryApi } from '@/lib/api/inventory'
import { formatMoney } from '@/lib/money'

export function ProductDetailPage() {
  const { orgId, slug } = useParams() as { orgId: string; slug: string }
  const [selectedSkuId, setSelectedSkuId] = useState<string | null>(null)
  const [qty, setQty] = useState(1)
  const [added, setAdded] = useState(false)

  const product = useQuery({
    queryKey: ['store', orgId, 'products', slug],
    queryFn: () => catalogApi.getStorefrontProduct(orgId, slug),
  })

  const skuIds = product.data?.skus.map((s) => s.id) ?? []
  // BFF-side composition (LLD-05 §6) is deferred -- this merges catalog
  // and inventory client-side instead of in one server round trip.
  // "Availability unknown" degrades to "assume available"; checkout still
  // validates for real against inventory-svc regardless.
  const availability = useQuery({
    queryKey: ['store', orgId, 'availability', skuIds],
    queryFn: () => inventoryApi.getAvailability(orgId, skuIds),
    enabled: skuIds.length > 0,
  })

  const sku = product.data?.skus.find((s) => s.id === selectedSkuId) ?? product.data?.skus[0]
  const inStock = availability.data?.items.find((a) => a.sku_id === sku?.id)?.in_stock ?? true

  const addToCart = useMutation({
    mutationFn: () => {
      if (!sku) throw new Error('no sku selected')
      return cartApi.setItem(orgId, sku.id, qty)
    },
    onSuccess: () => setAdded(true),
  })

  if (product.isLoading) return <p className="text-sm text-muted-foreground">Loading...</p>
  if (product.error) return <ErrorBanner error={product.error} />
  if (!product.data) return null

  return (
    <div className="max-w-2xl">
      <PageHeader title={product.data.name} />
      <ErrorBanner error={addToCart.error} />

      <div
        className="mb-4 max-w-none text-sm text-muted-foreground"
        // The server (LLD-05 §2) and search-svc both already sanitize this
        // with an allow-list; re-sanitizing client-side (LLD-15 §6's
        // <SafeHtml>, DOMPurify) is deferred -- documented gap, not an
        // oversight: this is the one dangerouslySetInnerHTML this epic adds.
        dangerouslySetInnerHTML={{ __html: product.data.description_html }}
      />

      {product.data.skus.length === 0 ? (
        <p className="text-sm text-muted-foreground">This product has no purchasable options yet.</p>
      ) : (
        <>
          <div className="mb-4 flex flex-col gap-2">
            {product.data.skus.map((s) => (
              <label
                key={s.id}
                className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm"
              >
                <input
                  type="radio"
                  name="sku"
                  checked={(selectedSkuId ?? product.data?.skus[0]?.id) === s.id}
                  onChange={() => setSelectedSkuId(s.id)}
                />
                <span className="flex-1">{s.sku_code}</span>
                <span>{formatMoney(s.price_minor)}</span>
              </label>
            ))}
          </div>

          {!inStock && <p className="mb-3 text-sm text-destructive">Out of stock</p>}

          <div className="flex items-center gap-3">
            <label htmlFor="qty" className="text-sm text-muted-foreground">
              Qty
            </label>
            <input
              id="qty"
              type="number"
              min={1}
              max={99}
              value={qty}
              onChange={(e) => setQty(Math.min(99, Math.max(1, Number(e.target.value) || 1)))}
              className="w-16 rounded-md border border-border bg-background px-2 py-1.5 text-sm"
            />
            <Button disabled={!inStock || addToCart.isPending} onClick={() => addToCart.mutate()}>
              Add to cart
            </Button>
            {added && <span className="text-sm text-muted-foreground">Added.</span>}
          </div>
        </>
      )}
    </div>
  )
}
