import { Link, Outlet } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { navigateToLogout, useRequireAuth } from '@/lib/session'

export function AccountLayout() {
  const { pending, session } = useRequireAuth()

  if (pending || !session.data) return null

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-48 flex-col gap-6 border-r border-border bg-sidebar px-4 py-5">
        <div className="px-0.5 text-sm font-medium">My account</div>
        <nav className="flex flex-col gap-0.5">
          <Link
            to="/account/orders"
            className="rounded-md px-2.5 py-1.5 text-sm text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
          >
            Orders
          </Link>
        </nav>
        <div className="mt-auto flex flex-col gap-2 border-t border-sidebar-border pt-4">
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
