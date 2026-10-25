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
const AccountLayout = lazy(() =>
  import('@/components/layout/AccountLayout').then((m) => ({ default: m.AccountLayout })),
)
const StorefrontLayout = lazy(() =>
  import('@/components/layout/StorefrontLayout').then((m) => ({ default: m.StorefrontLayout })),
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
const CatalogPage = lazy(() =>
  import('@/pages/console/CatalogPage').then((m) => ({ default: m.CatalogPage })),
)
const ProductAdminPage = lazy(() =>
  import('@/pages/console/ProductAdminPage').then((m) => ({ default: m.ProductAdminPage })),
)
const InventoryPage = lazy(() =>
  import('@/pages/console/InventoryPage').then((m) => ({ default: m.InventoryPage })),
)
const OrdersPage = lazy(() =>
  import('@/pages/console/OrdersPage').then((m) => ({ default: m.OrdersPage })),
)
const UsersPage = lazy(() =>
  import('@/pages/admin/UsersPage').then((m) => ({ default: m.UsersPage })),
)
const OrgsPage = lazy(() =>
  import('@/pages/admin/OrgsPage').then((m) => ({ default: m.OrgsPage })),
)
const BrowsePage = lazy(() =>
  import('@/pages/storefront/BrowsePage').then((m) => ({ default: m.BrowsePage })),
)
const ProductDetailPage = lazy(() =>
  import('@/pages/storefront/ProductDetailPage').then((m) => ({ default: m.ProductDetailPage })),
)
const CartPage = lazy(() =>
  import('@/pages/storefront/CartPage').then((m) => ({ default: m.CartPage })),
)
const CheckoutPage = lazy(() =>
  import('@/pages/storefront/CheckoutPage').then((m) => ({ default: m.CheckoutPage })),
)
const OrderHistoryPage = lazy(() =>
  import('@/pages/account/OrderHistoryPage').then((m) => ({ default: m.OrderHistoryPage })),
)
const OrderStatusPage = lazy(() =>
  import('@/pages/account/OrderStatusPage').then((m) => ({ default: m.OrderStatusPage })),
)

export const router = createBrowserRouter([
  { path: '/', element: <SignInPrompt /> },
  { path: '/console', element: <ConsoleIndex /> },
  {
    path: '/console/orgs/:orgId',
    element: <ConsoleLayout />,
    children: [
      { index: true, element: <Navigate to="members" replace /> },
      { path: 'catalog', element: <CatalogPage /> },
      { path: 'catalog/:productId', element: <ProductAdminPage /> },
      { path: 'inventory', element: <InventoryPage /> },
      { path: 'orders', element: <OrdersPage /> },
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
  {
    path: '/account',
    element: <AccountLayout />,
    children: [
      { index: true, element: <Navigate to="orders" replace /> },
      { path: 'orders', element: <OrderHistoryPage /> },
      { path: 'orders/:orderId', element: <OrderStatusPage /> },
    ],
  },
  {
    path: '/s/:orgId',
    element: <StorefrontLayout />,
    children: [
      { index: true, element: <BrowsePage /> },
      { path: 'products/:slug', element: <ProductDetailPage /> },
      { path: 'cart', element: <CartPage /> },
      { path: 'checkout', element: <CheckoutPage /> },
    ],
  },
])
