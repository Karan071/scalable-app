import { Link, Outlet, useParams } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { navigateToLogin, navigateToLogout, useSession } from '@/lib/session'

/** `/s/:orgId/*` (LLD-15 §2's `/s/:storeSlug/*`, simplified to the org id
 * every backend route actually takes -- there is no slug->org_id resolver
 * service yet, same deliberate gap as LLD-05's own deferred public quote
 * endpoint). Public: browsing and search need no session at all. */
export function StorefrontLayout() {
  const { orgId } = useParams() as { orgId: string }
  const session = useSession()
  const authenticated = session.data?.authenticated ?? false

  return (
    <div className="min-h-screen">
      <header className="flex items-center justify-between border-b border-border px-6 py-3">
        <Link to={`/s/${orgId}`} className="text-sm font-medium">
          Store
        </Link>
        <nav className="flex items-center gap-4 text-sm">
          {authenticated ? (
            <>
              <Link to={`/s/${orgId}/cart`} className="hover:underline">
                Cart
              </Link>
              <Link to="/account/orders" className="hover:underline">
                My orders
              </Link>
              <Button variant="ghost" size="sm" onClick={() => void navigateToLogout()}>
                Sign out
              </Button>
            </>
          ) : (
            <Button size="sm" onClick={() => navigateToLogin(window.location.pathname)}>
              Sign in
            </Button>
          )}
        </nav>
      </header>
      <main className="px-6 py-6">
        <Outlet />
      </main>
    </div>
  )
}
