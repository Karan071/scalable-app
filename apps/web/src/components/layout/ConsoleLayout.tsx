import { Link, Outlet, useParams } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { navigateToLogout, useRequireAuth } from '@/lib/session'

const NAV_ITEMS = [
  { to: 'catalog', label: 'Catalog' },
  { to: 'inventory', label: 'Inventory' },
  { to: 'orders', label: 'Orders' },
  { to: 'members', label: 'Members' },
  { to: 'invitations', label: 'Invitations' },
  { to: 'roles', label: 'Roles' },
]

export function ConsoleLayout() {
  const { orgId } = useParams()
  const { pending, session } = useRequireAuth()

  if (pending || !session.data) return null

  const memberships = session.data.memberships ?? []
  const current = memberships.find((m) => m.org_id === orgId)

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 flex-col gap-6 border-r border-border bg-sidebar px-4 py-5">
        <div>
          <label htmlFor="org-switcher" className="sr-only">
            Switch organization
          </label>
          <select
            id="org-switcher"
            value={orgId}
            onChange={(e) => {
              window.location.href = `/console/orgs/${e.target.value}/members`
            }}
            className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm"
          >
            {memberships.map((m) => (
              <option key={m.org_id} value={m.org_id}>
                {m.name ?? m.org_id}
              </option>
            ))}
          </select>
          {current && <p className="mt-1 px-0.5 text-xs text-muted-foreground">{current.roles.join(', ') || 'no roles'}</p>}
        </div>

        <nav className="flex flex-col gap-0.5">
          {NAV_ITEMS.map((item) => (
            <Link
              key={item.to}
              to={`/console/orgs/${orgId}/${item.to}`}
              className="rounded-md px-2.5 py-1.5 text-sm text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
            >
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="mt-auto flex flex-col gap-2 border-t border-sidebar-border pt-4">
          <Link to={`/s/${orgId}`} className="px-2.5 text-sm text-sidebar-foreground hover:underline">
            View storefront
          </Link>
          {(session.data.platform_roles?.length ?? 0) > 0 && (
            <Link to="/admin/orgs" className="px-2.5 text-sm text-sidebar-foreground hover:underline">
              Platform admin
            </Link>
          )}
          <div className="px-2.5 text-sm text-muted-foreground">
            {session.data.user?.display_name ?? session.data.user?.handle}
          </div>
          <Button variant="ghost" size="sm" className="justify-start" onClick={() => void navigateToLogout()}>
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
