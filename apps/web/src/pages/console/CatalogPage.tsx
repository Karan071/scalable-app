import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { ErrorBanner } from '@/components/ErrorBanner'
import { PageHeader } from '@/components/PageHeader'
import { StatusDot } from '@/components/StatusDot'
import { Button } from '@/components/ui/button'
import { catalogApi } from '@/lib/api/catalog'

export function CatalogPage() {
  const { orgId } = useParams() as { orgId: string }
  const queryClient = useQueryClient()
  const queryKey = ['org', orgId, 'products']
  const [slug, setSlug] = useState('')
  const [name, setName] = useState('')

  const products = useQuery({ queryKey, queryFn: () => catalogApi.listProducts(orgId) })
  const invalidate = () => queryClient.invalidateQueries({ queryKey })

  const create = useMutation({
    mutationFn: () => catalogApi.createProduct(orgId, { slug, name }),
    onSuccess: () => {
      setSlug('')
      setName('')
      invalidate()
    },
  })
  const publish = useMutation({
    mutationFn: (p: { id: string; version: number }) => catalogApi.publishProduct(orgId, p.id, p.version),
    onSuccess: invalidate,
  })
  const archive = useMutation({
    mutationFn: (p: { id: string; version: number }) => catalogApi.archiveProduct(orgId, p.id, p.version),
    onSuccess: invalidate,
  })

  return (
    <div>
      <PageHeader title="Catalog" />
      <ErrorBanner error={products.error ?? create.error ?? publish.error ?? archive.error} />

      <form
        className="mb-6 flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (slug && name) create.mutate()
        }}
      >
        <div>
          <label htmlFor="product-slug" className="mb-1 block text-xs text-muted-foreground">
            Slug
          </label>
          <input
            id="product-slug"
            required
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            className="rounded-md border border-border bg-background px-2.5 py-1.5 text-sm"
          />
        </div>
        <div className="flex-1">
          <label htmlFor="product-name" className="mb-1 block text-xs text-muted-foreground">
            Name
          </label>
          <input
            id="product-name"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm"
          />
        </div>
        <Button type="submit" disabled={create.isPending}>
          Create product
        </Button>
      </form>

      {products.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading...</p>
      ) : products.data?.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No products yet.</p>
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
            {products.data?.items.map((product) => (
              <tr key={product.id} className="border-b border-border/60">
                <td className="py-2">
                  <Link to={`/console/orgs/${orgId}/catalog/${product.id}`} className="hover:underline">
                    {product.name}
                  </Link>
                </td>
                <td className="py-2 font-mono text-xs">{product.slug}</td>
                <td className="py-2">
                  <StatusDot status={product.status} />
                </td>
                <td className="py-2 text-right">
                  {product.status === 'draft' && (
                    <button
                      className="text-sm text-muted-foreground hover:text-foreground hover:underline"
                      onClick={() => publish.mutate(product)}
                    >
                      Publish
                    </button>
                  )}
                  {product.status === 'active' && (
                    <button
                      className="text-sm text-destructive hover:underline"
                      onClick={() => archive.mutate(product)}
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
