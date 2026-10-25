import { Link, Navigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { navigateToLogin, useSession } from '@/lib/session'

export function SignInPrompt() {
  const session = useSession()

  if (session.isLoading) return null

  if (session.data?.authenticated) {
    const firstOrg = session.data.memberships?.[0]
    if (firstOrg) return <Navigate to={`/console/orgs/${firstOrg.org_id}/members`} replace />
    if ((session.data.platform_roles?.length ?? 0) > 0) {
      return <Navigate to="/admin/orgs" replace />
    }
    // A pure shopper, no staff memberships anywhere -- there's no public
    // store directory yet (same deliberate gap as the slug->org_id
    // resolver E3.7 doesn't build), so there's nowhere to redirect them
    // into. At least don't tell an already-signed-in person to sign in.
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4">
        <h1 className="text-lg font-medium">ScaleForge</h1>
        <p className="text-sm text-muted-foreground">You're signed in.</p>
        <Button asChild>
          <Link to="/account/orders">View my orders</Link>
        </Button>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4">
      <h1 className="text-lg font-medium">ScaleForge</h1>
      <p className="text-sm text-muted-foreground">Sign in to continue.</p>
      <Button onClick={() => navigateToLogin('/')}>Sign in</Button>
    </div>
  )
}
