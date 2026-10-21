import { errorMessage } from '@/lib/error-ux'

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null
  return (
    <div className="mb-4 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
      {errorMessage(error)}
    </div>
  )
}
