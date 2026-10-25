/** LLD-15 §4: one idempotency key per cart, generated on mount and kept in
 * `sessionStorage` (never `localStorage` -- it's gone once the browser tab
 * closes, matching a cart's own lifetime) so a reload mid-checkout reuses
 * it instead of minting a second one. */
export function getOrCreateIdempotencyKey(cartId: string): string {
  const storageKey = `checkout-idempotency-key:${cartId}`
  try {
    const existing = sessionStorage.getItem(storageKey)
    if (existing) return existing
    const created = crypto.randomUUID()
    sessionStorage.setItem(storageKey, created)
    return created
  } catch {
    // Private-browsing/blocked storage: still works, just doesn't survive a reload.
    return crypto.randomUUID()
  }
}
