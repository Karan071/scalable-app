import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useParams } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { StatusDot } from '@/components/StatusDot'
import { authzApi } from '@/lib/api/authz'
import { orgsApi } from '@/lib/api/orgs'

export function RolesPage() {
  const { orgId } = useParams() as { orgId: string }
  const queryClient = useQueryClient()
  const [membershipId, setMembershipId] = useState('')
  const [roleId, setRoleId] = useState('')

  const assignments = useQuery({
    queryKey: ['org', orgId, 'role-assignments'],
    queryFn: () => authzApi.listOrgRoleAssignments(orgId),
  })
  const members = useQuery({
    queryKey: ['org', orgId, 'members'],
    queryFn: () => orgsApi.listMembers(orgId),
  })
  const roles = useQuery({
    queryKey: ['roles'],
    queryFn: () => authzApi.listRoles(),
  })
  const orgRoles = roles.data?.filter((r) => r.scope === 'org') ?? []

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ['org', orgId, 'role-assignments'] })

  const assign = useMutation({
    mutationFn: () => {
      const member = members.data?.find((m) => m.id === membershipId)
      if (!member) throw new Error('select a member')
      return authzApi.assignOrgRole(orgId, member.user_id, roleId, membershipId)
    },
    onSuccess: invalidate,
  })
  const revoke = useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) =>
      authzApi.revokeOrgRole(orgId, id, version),
    onSuccess: invalidate,
  })

  return (
    <div>
      <PageHeader title="Roles" />
      <ErrorBanner error={assignments.error ?? assign.error ?? revoke.error} />

      <form
        className="mb-6 flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (membershipId && roleId) assign.mutate()
        }}
      >
        <div className="flex-1">
          <label htmlFor="assign-member" className="mb-1 block text-xs text-muted-foreground">
            Member
          </label>
          <select
            id="assign-member"
            required
            value={membershipId}
            onChange={(e) => setMembershipId(e.target.value)}
            className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm"
          >
            <option value="" disabled>
              Select a member
            </option>
            {members.data
              ?.filter((m) => m.status === 'active')
              .map((m) => (
                <option key={m.id} value={m.id}>
                  {m.user_id}
                </option>
              ))}
          </select>
        </div>
        <div>
          <label htmlFor="assign-role" className="mb-1 block text-xs text-muted-foreground">
            Role
          </label>
          <select
            id="assign-role"
            required
            value={roleId}
            onChange={(e) => setRoleId(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
          >
            <option value="" disabled>
              Select a role
            </option>
            {orgRoles.map((r) => (
              <option key={r.id} value={r.id}>
                {r.id}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" disabled={assign.isPending}>
          Assign
        </Button>
      </form>

      {assignments.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading role assignments...</p>
      ) : assignments.data?.length === 0 ? (
        <p className="text-sm text-muted-foreground">No roles assigned yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th className="py-2 font-normal">User</th>
              <th className="py-2 font-normal">Role</th>
              <th className="py-2 font-normal">Status</th>
              <th className="py-2 font-normal" />
            </tr>
          </thead>
          <tbody>
            {assignments.data?.map((assignment) => (
              <tr key={assignment.id} className="border-b border-border/60">
                <td className="py-2 font-mono text-xs">{assignment.user_id}</td>
                <td className="py-2">{assignment.role_id}</td>
                <td className="py-2">
                  <StatusDot status={assignment.status} />
                </td>
                <td className="py-2 text-right">
                  {assignment.status !== 'revoked' && (
                    <button
                      className="text-sm text-destructive hover:underline"
                      onClick={() => revoke.mutate({ id: assignment.id, version: assignment.version })}
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
