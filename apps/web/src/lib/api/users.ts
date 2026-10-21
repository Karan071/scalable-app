import { apiClient } from '../api-client'

export interface PlatformUser {
  id: string
  handle: string | null
  display_name: string | null
  email: string
  status: 'active' | 'disabled' | 'deletion_pending' | 'anonymized'
  version: number
}

export const usersApi = {
  listPlatformUsers: (search?: string) =>
    apiClient.get<PlatformUser[]>(
      `/api/users/v1/platform/users${search ? `?search=${encodeURIComponent(search)}` : ''}`,
    ),

  disablePlatformUser: (userId: string, reason: string) =>
    apiClient.post(`/api/users/v1/platform/users/${userId}:disable`, { reason }),

  reactivatePlatformUser: (userId: string) =>
    apiClient.post(`/api/users/v1/platform/users/${userId}:reactivate`),
}
