import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { StatusDot } from '@/components/StatusDot'
import { orgsApi } from '@/lib/api/orgs'

export function OrgsPage() {
  const queryClient = useQueryClient()

  const orgs = useQuery({
    queryKey: ['platform', 'orgs'],
    queryFn: () => orgsApi.listPlatformOrgs(),
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['platform', 'orgs'] })

  const suspend = useMutation({
    mutationFn: (orgId: string) => orgsApi.suspendOrg(orgId),
    onSuccess: invalidate,
  })
  const reactivate = useMutation({
    mutationFn: (orgId: string) => orgsApi.reactivateOrg(orgId),
    onSuccess: invalidate,
  })
  const archive = useMutation({
    mutationFn: (orgId: string) => orgsApi.archiveOrg(orgId),
    onSuccess: invalidate,
  })

  return (
    <div>
      <PageHeader title="Organizations" />
      <ErrorBanner error={orgs.error ?? suspend.error ?? reactivate.error ?? archive.error} />

      {orgs.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading organizations...</p>
      ) : orgs.data?.length === 0 ? (
        <p className="text-sm text-muted-foreground">No organizations yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th className="py-2 font-normal">Name</th>
              <th className="py-2 font-normal">Slug</th>
              <th className="py-2 font-normal">Status</th>
              <th className="py-2 font-normal" />
            </tr>
          </thead>
          <tbody>
            {orgs.data?.map((org) => (
              <tr key={org.id} className="border-b border-border/60">
                <td className="py-2">{org.name}</td>
                <td className="py-2 text-muted-foreground">{org.slug}</td>
                <td className="py-2">
                  <StatusDot status={org.status} />
                </td>
                <td className="py-2 text-right">
                  {org.status === 'active' && (
                    <button
                      className="text-sm text-muted-foreground hover:text-foreground hover:underline"
                      onClick={() => suspend.mutate(org.id)}
                    >
                      Suspend
                    </button>
                  )}
                  {org.status === 'suspended' && (
                    <button
                      className="text-sm text-muted-foreground hover:text-foreground hover:underline"
                      onClick={() => reactivate.mutate(org.id)}
                    >
                      Reactivate
                    </button>
                  )}
                  {org.status !== 'archived' && (
                    <button
                      className="ml-3 text-sm text-destructive hover:underline"
                      onClick={() => {
                        if (window.confirm('Archive this organization?')) archive.mutate(org.id)
                      }}
                    >
                      Archive
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
