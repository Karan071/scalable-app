import { cn } from '@/lib/utils'

/** A plain colored dot + label, not a pill/badge-for-everything pattern. */
const TONE_CLASSES: Record<'good' | 'warn' | 'bad' | 'neutral', string> = {
  good: 'bg-primary',
  warn: 'bg-amber-500',
  bad: 'bg-destructive',
  neutral: 'bg-muted-foreground',
}

const STATUS_TONE: Record<string, 'good' | 'warn' | 'bad' | 'neutral'> = {
  active: 'good',
  pending: 'warn',
  disabled: 'bad',
  suspended: 'bad',
  revoked: 'bad',
  archived: 'neutral',
  expired: 'neutral',
  accepted: 'good',
  deletion_pending: 'warn',
  anonymized: 'neutral',
  confirmed: 'good',
  processing: 'warn',
  shipped: 'good',
  delivered: 'good',
  cancelled: 'bad',
  refunded: 'neutral',
}

export function StatusDot({ status }: { status: string }) {
  const tone = STATUS_TONE[status] ?? 'neutral'
  return (
    <span className="inline-flex items-center gap-1.5 text-sm">
      <span className={cn('size-1.5 rounded-full', TONE_CLASSES[tone])} aria-hidden />
      {status.replace(/_/g, ' ')}
    </span>
  )
}
