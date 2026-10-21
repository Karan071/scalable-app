import { apiClient } from '../api-client'

export interface Member {
  id: string
  org_id: string
  user_id: string
  status: 'pending' | 'active' | 'disabled' | 'revoked'
  version: number
  org_name: string | null
  org_slug: string | null
}

export interface Invitation {
  id: string
  org_id: string
  email: string
  roles_requested: string[]
  status: 'pending' | 'accepted' | 'revoked' | 'expired'
  expires_at: string
}

export interface Organization {
  id: string
  slug: string
  name: string
  status: 'pending' | 'active' | 'suspended' | 'archived'
  currency: string
  version: number
}

export const orgsApi = {
  listMembers: (orgId: string) => apiClient.get<Member[]>(`/api/orgs/v1/orgs/${orgId}/members`),

  disableMember: (orgId: string, membershipId: string, reason?: string) =>
    apiClient.post(`/api/orgs/v1/orgs/${orgId}/members/${membershipId}:disable`, { reason }),

  reactivateMember: (orgId: string, membershipId: string, reason?: string) =>
    apiClient.post(`/api/orgs/v1/orgs/${orgId}/members/${membershipId}:reactivate`, { reason }),

  removeMember: (orgId: string, membershipId: string) =>
    apiClient.delete(`/api/orgs/v1/orgs/${orgId}/members/${membershipId}`),

  listInvitations: (orgId: string) =>
    apiClient.get<Invitation[]>(`/api/orgs/v1/orgs/${orgId}/invitations`),

  createInvitation: (orgId: string, email: string, rolesRequested: string[]) =>
    apiClient.post<{ invitation_id: string; expires_at: string }>(
      `/api/orgs/v1/orgs/${orgId}/invitations`,
      { email, roles_requested: rolesRequested },
    ),

  revokeInvitation: (orgId: string, invitationId: string) =>
    apiClient.delete(`/api/orgs/v1/orgs/${orgId}/invitations/${invitationId}`),

  listPlatformOrgs: () => apiClient.get<Organization[]>('/api/orgs/v1/platform/orgs'),

  suspendOrg: (orgId: string, reason?: string) =>
    apiClient.post<Organization>(`/api/orgs/v1/platform/orgs/${orgId}:suspend`, { reason }),

  reactivateOrg: (orgId: string, reason?: string) =>
    apiClient.post<Organization>(`/api/orgs/v1/platform/orgs/${orgId}:reactivate`, { reason }),

  archiveOrg: (orgId: string, reason?: string) =>
    apiClient.post<Organization>(`/api/orgs/v1/platform/orgs/${orgId}:archive`, { reason }),
}
