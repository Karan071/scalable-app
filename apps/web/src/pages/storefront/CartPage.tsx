import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { Button } from '@/components/ui/button'
import { cartApi } from '@/lib/api/cart'
import { catalogApi } from '@/lib/api/catalog'
import { formatMoney } from '@/lib/money'

export function CartPage() {
  const { orgId } = useParams() as { orgId: string }
  const queryClient = useQueryClient()
  const cartKey = ['store', orgId, 'cart']

  const cart = useQuery({ queryKey: cartKey, queryFn: () => cartApi.getCart(orgId) })

  const skuIds = cart.data?.items.map((i) => i.sku_id) ?? []
  const skus = useQuery({
    queryKey: ['store', orgId, 'skus', skuIds],
    queryFn: () => catalogApi.listStorefrontSkus(orgId, skuIds),
    enabled: skuIds.length > 0,
  })
  const skuById = new Map((skus.data?.items ?? []).map((s) => [s.sku_id, s]))

  const invalidate = () => queryClient.invalidateQueries({ queryKey: cartKey })
  const setQty = useMutation({
    mutationFn: ({ skuId, qty }: { skuId: string; qty: number }) =>
      cartApi.setItem(orgId, skuId, qty),
    onSuccess: invalidate,
  })
  const removeItem = useMutation({
    mutationFn: (skuId: string) => cartApi.removeItem(orgId, skuId),
    onSuccess: invalidate,
  })

  if (cart.isLoading) return <p className="text-sm text-muted-foreground">Loading...</p>

  const items = cart.data?.items ?? []
  const subtotalMinor = items.reduce((sum, item) => {
    const price = skuById.get(item.sku_id)?.price_minor ?? 0
    return sum + price * item.qty
  }, 0)

  return (
    <div className="max-w-2xl">
      <PageHeader title="Cart" />
      <ErrorBanner error={cart.error ?? setQty.error ?? removeItem.error} />

      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">Your cart is empty.</p>
      ) : (
        <>
          <div className="flex flex-col gap-3">
            {items.map((item) => {
              const info = skuById.get(item.sku_id)
              return (
                <div
                  key={item.sku_id}
                  className="flex items-center gap-3 rounded-md border border-border px-3 py-2 text-sm"
                >
                  <span className="flex-1">{info?.product_name ?? item.sku_id}</span>
                  <input
                    type="number"
                    min={1}
                    max={99}
                    value={item.qty}
                    onChange={(e) =>
                      setQty.mutate({
                        skuId: item.sku_id,
                        qty: Math.min(99, Math.max(1, Number(e.target.value) || 1)),
                      })
                    }
                    className="w-16 rounded-md border border-border bg-background px-2 py-1"
                  />
                  <span className="w-20 text-right">
                    {info ? formatMoney(info.price_minor * item.qty) : '--'}
                  </span>
                  <button
                    className="text-destructive hover:underline"
                    onClick={() => removeItem.mutate(item.sku_id)}
                  >
                    Remove
                  </button>
                </div>
              )
            })}
          </div>

          <div className="mt-4 flex items-center justify-between border-t border-border pt-4">
            <span className="text-sm text-muted-foreground">Subtotal</span>
            <span className="text-sm font-medium">{formatMoney(subtotalMinor)}</span>
          </div>

          <Button asChild className="mt-4">
            <Link to={`/s/${orgId}/checkout`}>Checkout</Link>
          </Button>
        </>
      )}
    </div>
  )
}
