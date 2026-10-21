import { Link, Outlet } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { navigateToLogout, useRequireAuth } from '@/lib/session'

const NAV_ITEMS = [
  { to: '/admin/orgs', label: 'Organizations' },
  { to: '/admin/users', label: 'Users' },
]

export function AdminLayout() {
  const { pending, session } = useRequireAuth()

  if (pending || !session.data) return null

  // Client-side only -- a UX hint, not a security control (LLD-15 §2). The
  // server re-checks every platform action independently.
  const isPlatformRole = (session.data.platform_roles?.length ?? 0) > 0

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 flex-col gap-6 border-r border-border bg-sidebar px-4 py-5">
        <div className="px-0.5 text-sm font-medium">Platform admin</div>

        {!isPlatformRole && (
          <p className="px-0.5 text-xs text-muted-foreground">
            Your account has no platform role -- these pages will show a permission error.
          </p>
        )}

        <nav className="flex flex-col gap-0.5">
          {NAV_ITEMS.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className="rounded-md px-2.5 py-1.5 text-sm text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
            >
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="mt-auto flex flex-col gap-2 border-t border-sidebar-border pt-4">
          <Link to="/console" className="px-2.5 text-sm text-sidebar-foreground hover:underline">
            Back to console
          </Link>
          <div className="px-2.5 text-sm text-muted-foreground">
            {session.data.user?.display_name ?? session.data.user?.handle}
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="justify-start"
            onClick={() => void navigateToLogout()}
          >
            Sign out
          </Button>
        </div>
      </aside>
      <main className="flex-1 px-8 py-6">
        <Outlet />
      </main>
    </div>
  )
}
