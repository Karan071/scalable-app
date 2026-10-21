import { lazy } from 'react'
import { createBrowserRouter, Navigate } from 'react-router-dom'

import { ConsoleIndex } from '@/pages/ConsoleIndex'
import { SignInPrompt } from '@/pages/SignInPrompt'

const ConsoleLayout = lazy(() =>
  import('@/components/layout/ConsoleLayout').then((m) => ({ default: m.ConsoleLayout })),
)
const AdminLayout = lazy(() =>
  import('@/components/layout/AdminLayout').then((m) => ({ default: m.AdminLayout })),
)
const MembersPage = lazy(() =>
  import('@/pages/console/MembersPage').then((m) => ({ default: m.MembersPage })),
)
const InvitationsPage = lazy(() =>
  import('@/pages/console/InvitationsPage').then((m) => ({ default: m.InvitationsPage })),
)
const RolesPage = lazy(() =>
  import('@/pages/console/RolesPage').then((m) => ({ default: m.RolesPage })),
)
const UsersPage = lazy(() =>
  import('@/pages/admin/UsersPage').then((m) => ({ default: m.UsersPage })),
)
const OrgsPage = lazy(() =>
  import('@/pages/admin/OrgsPage').then((m) => ({ default: m.OrgsPage })),
)

export const router = createBrowserRouter([
  { path: '/', element: <SignInPrompt /> },
  { path: '/console', element: <ConsoleIndex /> },
  {
    path: '/console/orgs/:orgId',
    element: <ConsoleLayout />,
    children: [
      { index: true, element: <Navigate to="members" replace /> },
      { path: 'members', element: <MembersPage /> },
      { path: 'invitations', element: <InvitationsPage /> },
      { path: 'roles', element: <RolesPage /> },
    ],
  },
  {
    path: '/admin',
    element: <AdminLayout />,
    children: [
      { index: true, element: <Navigate to="orgs" replace /> },
      { path: 'orgs', element: <OrgsPage /> },
      { path: 'users', element: <UsersPage /> },
    ],
  },
])
