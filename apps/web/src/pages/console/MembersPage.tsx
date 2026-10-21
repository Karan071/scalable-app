import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { StatusDot } from '@/components/StatusDot'
import { orgsApi } from '@/lib/api/orgs'

export function MembersPage() {
  const { orgId } = useParams() as { orgId: string }
  const queryClient = useQueryClient()

  const members = useQuery({
    queryKey: ['org', orgId, 'members'],
    queryFn: () => orgsApi.listMembers(orgId),
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['org', orgId, 'members'] })

  const disable = useMutation({
    mutationFn: (membershipId: string) => orgsApi.disableMember(orgId, membershipId),
    onSuccess: invalidate,
  })
  const reactivate = useMutation({
    mutationFn: (membershipId: string) => orgsApi.reactivateMember(orgId, membershipId),
    onSuccess: invalidate,
  })
  const remove = useMutation({
    mutationFn: (membershipId: string) => orgsApi.removeMember(orgId, membershipId),
    onSuccess: invalidate,
  })

  return (
    <div>
      <PageHeader title="Members" />
      <ErrorBanner error={members.error ?? disable.error ?? reactivate.error ?? remove.error} />

      {members.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading members...</p>
      ) : members.data?.length === 0 ? (
        <p className="text-sm text-muted-foreground">No members yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th className="py-2 font-normal">User</th>
              <th className="py-2 font-normal">Status</th>
              <th className="py-2 font-normal" />
            </tr>
          </thead>
          <tbody>
            {members.data?.map((member) => (
              <tr key={member.id} className="border-b border-border/60">
                <td className="py-2 font-mono text-xs">{member.user_id}</td>
                <td className="py-2">
                  <StatusDot status={member.status} />
                </td>
                <td className="py-2 text-right">
                  {member.status === 'active' && (
                    <button
                      className="text-sm text-muted-foreground hover:text-foreground hover:underline"
                      onClick={() => disable.mutate(member.id)}
                    >
                      Disable
                    </button>
                  )}
                  {member.status === 'disabled' && (
                    <button
                      className="text-sm text-muted-foreground hover:text-foreground hover:underline"
                      onClick={() => reactivate.mutate(member.id)}
                    >
                      Reactivate
                    </button>
                  )}
                  {member.status !== 'revoked' && (
                    <button
                      className="ml-3 text-sm text-destructive hover:underline"
                      onClick={() => {
                        if (window.confirm('Remove this member? This cannot be undone.')) {
                          remove.mutate(member.id)
                        }
                      }}
                    >
                      Remove
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
