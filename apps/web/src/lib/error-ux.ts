/**
 * Error UX mapping (LLD-15 §8), scoped to what E2.10's pages need.
 * 401 session_expired/unauthenticated/step_up_required are handled
 * globally (see query-client.ts); everything here is page-level.
 */

import { ApiError } from './api-client'

export type ErrorKind =
  | 'validation-error'
  | 'forbidden'
  | 'org-suspended'
  | 'not-found'
  | 'precondition-failed'
  | 'conflict'
  | 'rate-limited'
  | 'dependency-unavailable'
  | 'unknown'

export function classifyError(error: unknown): ErrorKind {
  if (!(error instanceof ApiError)) return 'unknown'
  const { status, title } = error.problem
  if (status === 422 && title === 'idempotency-key-reused') return 'conflict'
  if (status === 422 || status === 400) return 'validation-error'
  if (status === 403 && title === 'org-suspended') return 'org-suspended'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not-found'
  if (status === 412) return 'precondition-failed'
  if (status === 409 || status === 402) return 'conflict'
  if (status === 429) return 'rate-limited'
  if (status === 503) return 'dependency-unavailable'
  return 'unknown'
}

/** 409/422 problem titles this checkout/cart/order UI (E3.7) actually
 * produces (LLD-15 §8's `out-of-stock`/`cart-changed`/`payment-declined`
 * row, generalized to every real title rather than three hardcoded ones --
 * cart-svc, orders-svc, and inventory-svc each raise their own). Falls
 * back to a generic "refresh and retry" for anything not listed. */
const CONFLICT_MESSAGES: Record<string, string> = {
  'sku-unavailable': 'This item is no longer available.',
  'cart-full': 'Your cart is full (50 items max). Remove something before adding more.',
  'checkout-in-progress': 'A checkout for this cart is already running -- please wait.',
  'idempotency-key-reused': 'This action was already submitted with different details.',
  'invalid-transition': "That action isn't valid for this order's current status.",
  'below-reserved': "Can't reduce stock below what's already reserved.",
  'store-unavailable': "This store isn't ready to accept orders yet.",
  'out-of-stock': "One of your cart's items is now out of stock. Please review your cart.",
  'cart-changed': 'Your cart changed since checkout started -- please review it and try again.',
  'payment-declined': 'Your payment method was declined. Please try a different one.',
  'order-not-cancellable': 'This order can no longer be cancelled.',
  'order-not-refundable': 'Only a delivered order can be refunded.',
  'order-not-found': 'This order could not be found.',
}

export function errorMessage(error: unknown): string {
  switch (classifyError(error)) {
    case 'validation-error':
      return (error as ApiError).problem.detail ?? 'Please check the form and try again.'
    case 'forbidden':
      return "You don't have access to do that."
    case 'org-suspended':
      return 'This organization is suspended.'
    case 'not-found':
      return 'Not found.'
    case 'precondition-failed':
      return 'This item changed since you loaded it. Reload and try again.'
    case 'conflict':
      return (
        CONFLICT_MESSAGES[(error as ApiError).problem.title] ??
        'This changed since you loaded it -- please refresh and try again.'
      )
    case 'rate-limited':
      return 'Too many requests -- please wait a moment and try again.'
    case 'dependency-unavailable':
      return 'A dependent service is temporarily unavailable. Please try again shortly.'
    default:
      return error instanceof Error ? error.message : 'Something went wrong.'
  }
}
