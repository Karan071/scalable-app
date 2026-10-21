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
  | 'rate-limited'
  | 'dependency-unavailable'
  | 'unknown'

export function classifyError(error: unknown): ErrorKind {
  if (!(error instanceof ApiError)) return 'unknown'
  const { status, title } = error.problem
  if (status === 422 || status === 400) return 'validation-error'
  if (status === 403 && title === 'org-suspended') return 'org-suspended'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not-found'
  if (status === 412) return 'precondition-failed'
  if (status === 429) return 'rate-limited'
  if (status === 503) return 'dependency-unavailable'
  return 'unknown'
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
    case 'rate-limited':
      return 'Too many requests -- please wait a moment and try again.'
    case 'dependency-unavailable':
      return 'A dependent service is temporarily unavailable. Please try again shortly.'
    default:
      return error instanceof Error ? error.message : 'Something went wrong.'
  }
}
