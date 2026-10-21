import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'

import { getCsrfToken, setCsrfToken } from './api-client'

export interface Membership {
  org_id: string
  name: string | null
  roles: string[]
}

export interface SessionUser {
  id: string
  handle: string | null
  display_name: string | null
}

export interface SessionData {
  authenticated: boolean
  user?: SessionUser
  csrf_token?: string
  memberships?: Membership[]
  platform_roles?: string[]
  acr?: string
  session_class?: string
}

export const SESSION_QUERY_KEY = ['session'] as const

async function fetchSession(): Promise<SessionData> {
  const resp = await fetch('/auth/session', { credentials: 'same-origin' })
  if (!resp.ok) return { authenticated: false }
  return (await resp.json()) as SessionData
}

/** Session bootstrap (LLD-01 §10). `staleTime: Infinity` -- invalidated
 * explicitly after login-return/logout/step-up-return, not polled. */
export function useSession() {
  const query = useQuery({
    queryKey: SESSION_QUERY_KEY,
    queryFn: fetchSession,
    staleTime: Infinity,
  })

  useEffect(() => {
    setCsrfToken(query.data?.csrf_token ?? null)
  }, [query.data?.csrf_token])

  return query
}

/** Full-page redirect to /auth/login when unauthenticated (LLD-15 §3 -- the
 * SPA never handles credentials, so this is a real navigation, not a
 * client-side route change). Returns true while the guard is still
 * resolving or redirecting, so the caller can render nothing meanwhile. */
export function useRequireAuth(): { pending: boolean; session: ReturnType<typeof useSession> } {
  const session = useSession()
  const authenticated = session.data?.authenticated ?? false

  useEffect(() => {
    if (!session.isLoading && !authenticated) {
      navigateToLogin(window.location.pathname)
    }
  }, [session.isLoading, authenticated])

  return { pending: session.isLoading || !authenticated, session }
}

export function useInvalidateSession() {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: SESSION_QUERY_KEY })
}

export function navigateToLogin(returnTo: string): void {
  window.location.href = `/auth/login?return_to=${encodeURIComponent(returnTo)}`
}

export function navigateToStepUp(returnTo: string): void {
  window.location.href = `/auth/step-up?return_to=${encodeURIComponent(returnTo)}`
}

export async function navigateToLogout(): Promise<void> {
  // A plain <form> POST can't carry the X-CSRF-Token header the BFF
  // requires. `fetch` (default redirect: 'follow') runs the whole chain
  // itself -- BFF deletes the local session, then 303s to Keycloak's
  // end-session endpoint (which terminates the IdP session server-side via
  // `id_token_hint`, not needing the browser's own Keycloak cookie), which
  // 302s back to `post_logout_redirect_uri` (the app origin). `resp.url`
  // reflects that final resting URL even though the fetched body itself is
  // discarded -- a real top-level navigation there completes logout
  // visibly, matching what a form submission would have shown.
  const resp = await fetch('/auth/logout', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'x-csrf-token': getCsrfToken() ?? '' },
  })
  window.location.href = resp.url || '/'
}
