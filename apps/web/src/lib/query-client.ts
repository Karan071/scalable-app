/**
 * Global TanStack Query client with centralized 401/step-up handling
 * (LLD-15 §3). Both are full-page navigations, not client-side route
 * changes -- the SPA never handles credentials.
 */

import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'

import { ApiError } from './api-client'
import { navigateToLogin, navigateToStepUp } from './session'

function handleGlobalError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  const { status, title, step_up_url } = error.problem

  if (status === 401 && (title === 'session_expired' || title === 'unauthenticated')) {
    navigateToLogin(window.location.pathname + window.location.search)
    return true
  }
  if (status === 401 && title === 'step_up_required') {
    navigateToStepUp(window.location.pathname + window.location.search)
    return true
  }
  // step_up_url present but a differently-titled 401 -- still honor it.
  if (status === 401 && step_up_url) {
    navigateToStepUp(window.location.pathname + window.location.search)
    return true
  }
  return false
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        handleGlobalError(error)
      },
    }),
    mutationCache: new MutationCache({
      onError: (error) => {
        handleGlobalError(error)
      },
    }),
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  })
}
