import { Navigate } from 'react-router-dom'

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
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4">
      <h1 className="text-lg font-medium">ScaleForge</h1>
      <p className="text-sm text-muted-foreground">Sign in to continue.</p>
      <Button onClick={() => navigateToLogin('/')}>Sign in</Button>
    </div>
  )
}
