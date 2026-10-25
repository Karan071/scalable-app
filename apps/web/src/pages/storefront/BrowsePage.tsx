import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { searchApi } from '@/lib/api/search'
import { formatMoney } from '@/lib/money'
import { useDebouncedValue } from '@/lib/use-debounced-value'

/** LLD-15 §5: empty/short query is browse (no `q` filter at all), not a
 * search miss. Category/price/sort filters and the categories sidebar are
 * deferred -- `q` is the only filter wired to the URL for now. */
const MIN_QUERY_LENGTH = 2
const MAX_QUERY_LENGTH = 100

export function BrowsePage() {
  const { orgId } = useParams() as { orgId: string }
  const [params, setParams] = useSearchParams()
  const [input, setInput] = useState(params.get('q') ?? '')
  const debounced = useDebouncedValue(input)
  const q = debounced.length >= MIN_QUERY_LENGTH ? debounced : undefined

  const results = useQuery({
    queryKey: ['store', orgId, 'search', q ?? ''],
    queryFn: ({ signal }) => searchApi.search(orgId, { q }, signal),
    placeholderData: (previous) => previous,
  })

  return (
    <div>
      <PageHeader title="Browse" />

      <input
        value={input}
        onChange={(e) => {
          const next = e.target.value
          setInput(next)
          setParams(next ? { q: next } : {}, { replace: true })
        }}
        maxLength={MAX_QUERY_LENGTH}
        placeholder="Search products..."
        className="mb-6 w-full max-w-sm rounded-md border border-border bg-background px-3 py-2 text-sm"
      />

      <ErrorBanner error={results.error} />

      {results.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading...</p>
      ) : results.data?.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No products found.</p>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {results.data?.items.map((hit) => (
            <Link
              key={hit.product_id}
              to={`/s/${orgId}/products/${hit.slug}`}
              className="rounded-lg border border-border p-4 transition-colors hover:border-foreground/30"
            >
              <div className="mb-2 aspect-square rounded-md bg-muted" />
              <div className="text-sm font-medium">{hit.name}</div>
              <div className="text-sm text-muted-foreground">
                {hit.min_price_minor != null ? formatMoney(hit.min_price_minor) : '--'}
              </div>
              {!hit.in_stock && <div className="text-xs text-destructive">Out of stock</div>}
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
