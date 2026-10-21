import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { StatusDot } from '@/components/StatusDot'
import { usersApi } from '@/lib/api/users'

export function UsersPage() {
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')

  const users = useQuery({
    queryKey: ['platform', 'users', search],
    queryFn: () => usersApi.listPlatformUsers(search || undefined),
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['platform', 'users'] })

  const disable = useMutation({
    mutationFn: (userId: string) => {
      const reason = window.prompt('Reason for disabling this user (min 10 characters):')
      if (!reason || reason.length < 10) throw new Error('a reason of at least 10 characters is required')
      return usersApi.disablePlatformUser(userId, reason)
    },
    onSuccess: invalidate,
  })
  const reactivate = useMutation({
    mutationFn: (userId: string) => usersApi.reactivatePlatformUser(userId),
    onSuccess: invalidate,
  })

  return (
    <div>
      <PageHeader title="Users" />
      <ErrorBanner error={users.error ?? disable.error ?? reactivate.error} />

      <input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="Search by handle or email"
        className="mb-4 w-72 rounded-md border border-border bg-background px-2.5 py-1.5 text-sm"
      />

      {users.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading users...</p>
      ) : users.data?.length === 0 ? (
        <p className="text-sm text-muted-foreground">No users found.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th className="py-2 font-normal">Handle</th>
              <th className="py-2 font-normal">Email</th>
              <th className="py-2 font-normal">Status</th>
              <th className="py-2 font-normal" />
            </tr>
          </thead>
          <tbody>
            {users.data?.map((user) => (
              <tr key={user.id} className="border-b border-border/60">
                <td className="py-2">{user.handle ?? '—'}</td>
                <td className="py-2">{user.email}</td>
                <td className="py-2">
                  <StatusDot status={user.status} />
                </td>
                <td className="py-2 text-right">
                  {user.status === 'active' && (
                    <button
                      className="text-sm text-destructive hover:underline"
                      onClick={() => disable.mutate(user.id)}
                    >
                      Disable
                    </button>
                  )}
                  {user.status === 'disabled' && (
                    <button
                      className="text-sm text-muted-foreground hover:text-foreground hover:underline"
                      onClick={() => reactivate.mutate(user.id)}
                    >
                      Reactivate
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
