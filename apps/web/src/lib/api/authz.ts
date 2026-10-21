import { apiClient } from '../api-client'

export interface RoleAssignment {
  id: string
  user_id: string
  role_id: string
  domain: string
  org_id: string | null
  status: 'active' | 'suspended' | 'revoked'
  granted_by: string
  version: number
}

export interface Role {
  id: string
  scope: 'platform' | 'org' | 'customer'
  description: string
  requires_mfa: boolean
}

export const authzApi = {
  listRoles: () => apiClient.get<Role[]>('/api/authz/v1/roles'),

  listOrgRoleAssignments: (orgId: string) =>
    apiClient.get<RoleAssignment[]>(`/api/authz/v1/orgs/${orgId}/role-assignments`),

  assignOrgRole: (orgId: string, userId: string, roleId: string, membershipId: string) =>
    apiClient.post<RoleAssignment>(`/api/authz/v1/orgs/${orgId}/role-assignments`, {
      user_id: userId,
      role_id: roleId,
      membership_id: membershipId,
    }),

  revokeOrgRole: (orgId: string, assignmentId: string, version: number, reason?: string) =>
    apiClient.delete(
      `/api/authz/v1/orgs/${orgId}/role-assignments/${assignmentId}` +
        (reason ? `?${new URLSearchParams({ reason }).toString()}` : ''),
      { 'if-match': String(version) },
    ),

  listPlatformRoleAssignments: (userId?: string) =>
    apiClient.get<RoleAssignment[]>(
      `/api/authz/v1/platform/role-assignments${userId ? `?user_id=${userId}` : ''}`,
    ),

  assignPlatformRole: (userId: string, roleId: string, reason: string) =>
    apiClient.post<RoleAssignment>('/api/authz/v1/platform/role-assignments', {
      user_id: userId,
      role_id: roleId,
      reason,
    }),

  revokePlatformRole: (assignmentId: string, version: number, reason?: string) =>
    apiClient.delete(
      `/api/authz/v1/platform/role-assignments/${assignmentId}` +
        (reason ? `?${new URLSearchParams({ reason }).toString()}` : ''),
      { 'if-match': String(version) },
    ),
}
