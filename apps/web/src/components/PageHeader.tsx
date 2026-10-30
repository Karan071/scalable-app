import type { ReactNode } from 'react'

export function PageHeader({ title, action }: { title: string; action?: ReactNode }) {
  return (
    <div className="mb-5 flex items-center justify-between border-b border-border pb-3">
      <h1 className="font-heading text-xl font-semibold tracking-tight">{title}</h1>
      {action}
    </div>
  )
}
