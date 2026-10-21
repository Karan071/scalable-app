import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useParams } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { StatusDot } from '@/components/StatusDot'
import { orgsApi } from '@/lib/api/orgs'

const INVITABLE_ROLES = ['org_admin', 'catalog_manager', 'order_fulfiller', 'support_agent']

export function InvitationsPage() {
  const { orgId } = useParams() as { orgId: string }
  const queryClient = useQueryClient()
  const [email, setEmail] = useState('')
  const [role, setRole] = useState(INVITABLE_ROLES[0])

  const invitations = useQuery({
    queryKey: ['org', orgId, 'invitations'],
    queryFn: () => orgsApi.listInvitations(orgId),
  })

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ['org', orgId, 'invitations'] })

  const create = useMutation({
    mutationFn: () => orgsApi.createInvitation(orgId, email, [role]),
    onSuccess: () => {
      setEmail('')
      invalidate()
    },
  })
  const revoke = useMutation({
    mutationFn: (invitationId: string) => orgsApi.revokeInvitation(orgId, invitationId),
    onSuccess: invalidate,
  })

  return (
    <div>
      <PageHeader title="Invitations" />
      <ErrorBanner error={invitations.error ?? create.error ?? revoke.error} />

      <form
        className="mb-6 flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (email) create.mutate()
        }}
      >
        <div className="flex-1">
          <label htmlFor="invite-email" className="mb-1 block text-xs text-muted-foreground">
            Email
          </label>
          <input
            id="invite-email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@example.com"
            className="w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm"
          />
        </div>
        <div>
          <label htmlFor="invite-role" className="mb-1 block text-xs text-muted-foreground">
            Role
          </label>
          <select
            id="invite-role"
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
          >
            {INVITABLE_ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" disabled={create.isPending}>
          Send invitation
        </Button>
      </form>

      {invitations.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading invitations...</p>
      ) : invitations.data?.length === 0 ? (
        <p className="text-sm text-muted-foreground">No invitations yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th className="py-2 font-normal">Email</th>
              <th className="py-2 font-normal">Roles requested</th>
              <th className="py-2 font-normal">Status</th>
              <th className="py-2 font-normal" />
            </tr>
          </thead>
          <tbody>
            {invitations.data?.map((invitation) => (
              <tr key={invitation.id} className="border-b border-border/60">
                <td className="py-2">{invitation.email}</td>
                <td className="py-2">{invitation.roles_requested.join(', ')}</td>
                <td className="py-2">
                  <StatusDot status={invitation.status} />
                </td>
                <td className="py-2 text-right">
                  {invitation.status === 'pending' && (
                    <button
                      className="text-sm text-destructive hover:underline"
                      onClick={() => revoke.mutate(invitation.id)}
                    >
                      Revoke
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
