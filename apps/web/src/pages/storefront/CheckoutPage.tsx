import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { Button } from '@/components/ui/button'
import { cartApi } from '@/lib/api/cart'
import { ordersApi } from '@/lib/api/orders'
import { getOrCreateIdempotencyKey } from '@/lib/checkout-idempotency'

/** mock-payment-provider's own scenario tokens (LLD-06 §8.1) -- there is
 * no real card form in this sandbox, so the "payment method" the shopper
 * picks here is directly one of the mock PSP's documented behaviors. */
const MOCK_PAYMENT_TOKENS = [
  { value: 'pm_mock_success', label: 'Test card -- succeeds' },
  { value: 'pm_mock_decline', label: 'Test card -- declined' },
]

export function CheckoutPage() {
  const { orgId } = useParams() as { orgId: string }
  const navigate = useNavigate()

  const cart = useQuery({ queryKey: ['store', orgId, 'cart'], queryFn: () => cartApi.getCart(orgId) })

  const [name, setName] = useState('')
  const [line1, setLine1] = useState('')
  const [city, setCity] = useState('')
  const [postalCode, setPostalCode] = useState('')
  const [country, setCountry] = useState('US')
  const [paymentToken, setPaymentToken] = useState(MOCK_PAYMENT_TOKENS[0].value)

  const checkout = useMutation({
    mutationFn: () => {
      if (!cart.data) throw new Error('cart not loaded')
      // LLD-15 §4: one key per cart, reused across retries/reloads.
      const idempotencyKey = getOrCreateIdempotencyKey(cart.data.id)
      return ordersApi.checkout(orgId, idempotencyKey, {
        cart_version: cart.data.version,
        shipping_address: {
          name,
          line1,
          city,
          postal_code: postalCode,
          country,
        },
        payment_method_token: paymentToken,
      })
    },
    onSuccess: (result) => navigate(`/account/orders/${result.order_id}`),
  })

  if (cart.isLoading) return <p className="text-sm text-muted-foreground">Loading...</p>
  if (cart.data && cart.data.items.length === 0) {
    return <p className="text-sm text-muted-foreground">Your cart is empty.</p>
  }

  return (
    <div className="max-w-md">
      <PageHeader title="Checkout" />
      <ErrorBanner error={cart.error ?? checkout.error} />

      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          checkout.mutate()
        }}
      >
        <Field label="Name" value={name} onChange={setName} />
        <Field label="Address" value={line1} onChange={setLine1} />
        <Field label="City" value={city} onChange={setCity} />
        <Field label="Postal code" value={postalCode} onChange={setPostalCode} />
        <Field label="Country (2 letters)" value={country} onChange={setCountry} maxLength={2} />

        <div>
          <label htmlFor="payment-token" className="mb-1 block text-xs text-muted-foreground">
            Payment method
          </label>
          <select
            id="payment-token"
            value={paymentToken}
            onChange={(e) => setPaymentToken(e.target.value)}
            className="w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm"
          >
            {MOCK_PAYMENT_TOKENS.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </div>

        <Button type="submit" disabled={checkout.isPending} className="mt-2">
          {checkout.isPending ? 'Placing order...' : 'Place order'}
        </Button>
      </form>
    </div>
  )
}

function Field({
  label,
  value,
  onChange,
  maxLength,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  maxLength?: number
}) {
  return (
    <div>
      <label className="mb-1 block text-xs text-muted-foreground">{label}</label>
      <input
        required
        value={value}
        maxLength={maxLength}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm"
      />
    </div>
  )
}
