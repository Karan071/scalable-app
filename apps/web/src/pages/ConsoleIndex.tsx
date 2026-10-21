import { Navigate } from 'react-router-dom'

import { useRequireAuth } from '@/lib/session'

/** `/console` with no :orgId yet -- redirects to the first membership's
 * console, or back to the sign-in prompt if the user has none. */
export function ConsoleIndex() {
  const { pending, session } = useRequireAuth()

  if (pending || !session.data) return null

  const firstOrg = session.data.memberships?.[0]
  return <Navigate to={firstOrg ? `/console/orgs/${firstOrg.org_id}/members` : '/'} replace />
}
