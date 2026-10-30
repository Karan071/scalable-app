import type { ReactNode } from 'react'
import {
  ArrowDown,
  ArrowRight,
  Check,
  ShieldCheck,
  Workflow,
} from 'lucide-react'
import { Link, Navigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { navigateToLogin, useSession } from '@/lib/session'

const theme = {
  ivory: '#FBF4E6',
  ivoryDeep: '#F3E9D4',
  ink: '#2A2420',
  inkMuted: '#6B6256',
  emerald: '#1F6D4C',
  vermilion: '#B9573D',
}

function SiteHeader({ action }: { action: ReactNode }) {
  return (
    <header className="sticky top-0 z-20 border-b bg-[#fbf4e6]/95 backdrop-blur" style={{ borderColor: theme.ivoryDeep }}>
      <div className="mx-auto flex h-[72px] max-w-7xl items-center justify-between px-5 sm:px-8">
        <a href="#top" className="rounded-sm focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-emerald-800" aria-label="ScaleForge home">
          <span className="font-sans text-xl font-semibold tracking-tight" style={{ color: theme.ink }}>ScaleForge</span>
        </a>
        <nav aria-label="Main navigation" className="hidden items-center gap-8 text-sm font-medium md:flex" style={{ color: theme.inkMuted }}>
          <a href="#platform" className="transition-colors hover:text-[#1f6d4c]">Platform</a>
          <a href="#architecture" className="transition-colors hover:text-[#1f6d4c]">Architecture</a>
          <a href="#principles" className="transition-colors hover:text-[#1f6d4c]">Principles</a>
        </nav>
        {action}
      </div>
    </header>
  )
}

function FlowNode({ eyebrow, title, detail, tone = 'paper' }: {
  eyebrow: string
  title: string
  detail: string
  tone?: 'paper' | 'green' | 'dark'
}) {
  const colors = tone === 'green'
    ? 'border-[#1f6d4c] bg-[#1f6d4c] text-[#fffaf0]'
    : tone === 'dark'
      ? 'border-[#3a342d] bg-[#3a342d] text-[#fffaf0]'
      : 'border-[#e5d9c3] bg-white/80 text-[#2a2420]'
  const secondary = tone === 'paper' ? 'text-[#74695c]' : 'text-white/70'

  return (
    <div className={`min-w-0 rounded-xl border p-4 shadow-[0_8px_24px_rgba(54,42,25,0.045)] sm:p-5 ${colors}`}>
      <p className={`mb-3 font-sans text-[11px] font-semibold uppercase tracking-[0.07em] ${secondary}`}>{eyebrow}</p>
      <h3 className="font-sans text-base font-semibold leading-snug">{title}</h3>
      <p className={`mt-2 text-[13px] leading-relaxed ${secondary}`}>{detail}</p>
    </div>
  )
}

function Connector({ vertical = false }: { vertical?: boolean }) {
  return (
    <div aria-hidden="true" className={`flex items-center justify-center text-[#a99c87] ${vertical ? 'py-1 md:hidden' : 'hidden md:flex'}`}>
      {vertical ? <ArrowDown size={16} /> : <ArrowRight size={17} />}
    </div>
  )
}

function ArchitectureDiagram() {
  return (
    <div className="rounded-2xl border border-[#e5d9c3] bg-[#f7efdf] p-4 sm:p-7 lg:p-9">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="font-sans text-xs font-semibold uppercase tracking-[0.08em] text-[#1f6d4c]">Request path</p>
          <p className="mt-1 text-sm leading-relaxed text-[#6b6256]">A tenant scoped journey from storefront to durable data.</p>
        </div>
        <span className="rounded-full border border-[#d9ccb5] bg-[#fbf4e6] px-3 py-1.5 text-xs font-medium text-[#62584d]">Tenant boundaries</span>
      </div>

      <div className="grid items-stretch gap-2 md:grid-cols-[1fr_28px_1fr_28px_1fr_28px_1fr]">
        <FlowNode eyebrow="01 · client" title="Storefront" detail="React app · browse, cart, checkout" />
        <Connector /><Connector vertical />
        <FlowNode eyebrow="02 · edge" title="API gateway" detail="Envoy · routing, limits, request IDs" />
        <Connector /><Connector vertical />
        <FlowNode eyebrow="03 · app" title="Commerce API" detail="FastAPI · tenant aware services" tone="green" />
        <Connector /><Connector vertical />
        <FlowNode eyebrow="04 · storage" title="PostgreSQL" detail="Tenant data · orders, catalog, stock" tone="dark" />
      </div>

      <div className="my-5 flex items-center gap-3" aria-hidden="true">
        <span className="h-px flex-1 bg-[#dfd2bc]" />
        <span className="font-sans text-xs font-semibold uppercase tracking-[0.08em] text-[#8b7f6e]">Platform services</span>
        <span className="h-px flex-1 bg-[#dfd2bc]" />
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ['Keycloak', 'OIDC identity and staff access'],
          ['RabbitMQ', 'Events for asynchronous follow up'],
          ['Redis', 'Sessions and short lived state'],
          ['Payment adapter', 'Replaceable provider integration'],
        ].map(([name, detail]) => (
          <div key={name} className="min-h-24 rounded-xl border border-[#e5d9c3] bg-[#fbf4e6] p-4 sm:p-5">
            <p className="text-sm font-semibold text-[#2a2420]">{name}</p>
            <p className="mt-2 text-[13px] leading-relaxed text-[#74695c]">{detail}</p>
          </div>
        ))}
      </div>
      <p className="mt-5 text-xs leading-relaxed text-[#74695c]">Architecture overview · components represent the platform design; integrations and deployment configuration may vary by environment.</p>
    </div>
  )
}

function LandingPage({ action }: { action: ReactNode }) {
  return (
    <div id="top" className="min-h-dvh overflow-hidden font-body" style={{ backgroundColor: theme.ivory, color: theme.ink }}>
      <SiteHeader action={action} />
      <main>
        <section className="relative mx-auto grid max-w-7xl items-center gap-12 px-5 pb-16 pt-16 sm:px-8 sm:pb-20 sm:pt-24 lg:grid-cols-[1.05fr_0.95fr] lg:gap-16 lg:pb-28 lg:pt-28">
          <div className="pointer-events-none absolute -right-32 -top-40 size-[520px] rounded-full border border-[#eadfc9] opacity-70" />
          <div className="pointer-events-none absolute -right-12 -top-20 size-[350px] rounded-full border border-[#eadfc9] opacity-70" />
          <div className="relative z-10">
            <p className="mb-6 flex items-center gap-2 font-sans text-[13px] font-semibold uppercase tracking-[0.06em] text-[#1f6d4c]"><span className="size-1.5 rounded-full bg-[#b9573d]" /> Commerce infrastructure, thoughtfully designed</p>
            <h1 className="max-w-[760px] font-sans text-[clamp(3.25rem,7vw,6rem)] font-semibold leading-[0.98] tracking-[-0.035em]">Build for the<br /><span className="text-[#1f6d4c]">long haul.</span></h1>
            <p className="mt-7 max-w-xl text-base leading-7 text-[#6b6256] sm:text-lg sm:leading-8">A multi tenant commerce platform designed to keep storefronts moving as teams, catalogs, and order volume grow.</p>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              {action}
              <a href="#architecture" className="inline-flex h-10 items-center gap-2 rounded-lg border border-[#d9ccb5] px-4 text-sm font-semibold text-[#39322a] transition-colors hover:bg-white/60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-800">Explore the architecture <ArrowRight size={15} /></a>
            </div>
            <div className="mt-10 flex flex-wrap gap-x-6 gap-y-3 border-t border-[#e5d9c3] pt-5 text-xs font-medium text-[#6b6256]">
              <span className="inline-flex items-center gap-2"><Check size={14} className="text-[#1f6d4c]" />Tenant aware by design</span>
              <span className="inline-flex items-center gap-2"><Check size={14} className="text-[#1f6d4c]" />Recoverable checkout flows</span>
            </div>
          </div>
          <div className="relative z-10 lg:pl-4">
            <div className="absolute -inset-4 rotate-2 rounded-[28px] bg-[#e9ddc6]" />
            <div className="relative rounded-[22px] border border-[#e5d9c3] bg-[#fffaf0] p-5 shadow-[0_24px_70px_rgba(50,39,22,0.10)] sm:p-7">
              <div className="flex items-center justify-between gap-4 border-b border-[#eee4d2] pb-4">
                <div><p className="font-sans text-xs font-semibold uppercase tracking-[0.08em] text-[#8b7f6e]">Order lifecycle</p><p className="mt-1.5 font-sans text-xl font-semibold leading-snug sm:text-2xl">A checkout that can recover</p></div>
                <span className="grid size-10 place-items-center rounded-xl bg-[#eaf0e7] text-[#1f6d4c]"><Workflow size={19} /></span>
              </div>
              <div className="space-y-0 py-3">
                {[
                  ['01', 'Snapshot cart and quote', 'Price is fixed for the checkout attempt'],
                  ['02', 'Reserve inventory', 'Stock is held before payment'],
                  ['03', 'Authorize payment', 'Provider response is recorded'],
                  ['04', 'Confirm or compensate', 'Failures trigger follow up and release'],
                ].map(([number, title, detail], index) => (
                  <div className="flex gap-4" key={number}>
                    <div className="flex flex-col items-center"><span className={`grid size-8 shrink-0 place-items-center rounded-full font-mono text-[10px] ${index === 3 ? 'bg-[#1f6d4c] text-white' : 'bg-[#f3e9d4] text-[#1f6d4c]'}`}>{number}</span>{index < 3 && <span className="my-1 h-7 w-px bg-[#dfd2bc]" />}</div>
                    <div className="pb-4 pt-0.5"><p className="text-sm font-semibold leading-snug">{title}</p><p className="mt-1 text-[13px] leading-relaxed text-[#74695c]">{detail}</p></div>
                  </div>
                ))}
              </div>
              <div className="flex items-center gap-2 rounded-lg bg-[#f3e9d4]/70 px-3 py-2.5 text-xs leading-relaxed text-[#62584d]"><ShieldCheck size={15} className="shrink-0 text-[#1f6d4c]" />Idempotent steps help prevent duplicate work when requests retry.</div>
            </div>
          </div>
        </section>

        <section id="platform" className="border-y border-[#e5d9c3] bg-[#f7efdf]">
          <div className="mx-auto max-w-7xl px-5 py-14 sm:px-8 sm:py-16">
            <div className="mb-8 max-w-2xl sm:mb-10"><p className="font-sans text-xs font-semibold uppercase tracking-[0.08em] text-[#1f6d4c]">One platform, clear boundaries</p><h2 className="mt-3 font-sans text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">Commerce operations in one place.</h2></div>
            <div className="grid gap-4 sm:grid-cols-3">
              {[
                { title: 'Sell', copy: 'Give every merchant a storefront for products, carts, and orders.' },
                  { title: 'Operate', copy: 'Manage catalog, inventory, and fulfillment with the team.' },
                  { title: 'Separate', copy: 'Keep organization data and staff access scoped by tenant.' },
                ].map((item, index) => <article key={item.title} className="border-t-2 border-[#1f6d4c] bg-[#fbf4e6] px-5 py-5 sm:px-6 sm:py-6"><p className="font-sans text-xs font-semibold tracking-wide text-[#8b7f6e]">0{index + 1}</p><h3 className="mt-4 font-sans text-xl font-semibold tracking-tight">{item.title}</h3><p className="mt-2 max-w-sm text-sm leading-6 text-[#6b6256]">{item.copy}</p></article>)}
            </div>
          </div>
        </section>

        <section id="architecture" className="mx-auto max-w-7xl px-5 py-16 sm:px-8 sm:py-24">
          <div className="mb-8 flex flex-col justify-between gap-4 sm:mb-10 sm:flex-row sm:items-end">
            <div><p className="font-sans text-xs font-semibold uppercase tracking-[0.08em] text-[#1f6d4c]">Under the hood</p><h2 className="mt-3 font-sans text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">A clear path through the system.</h2></div>
            <p className="max-w-md text-sm leading-6 text-[#6b6256]">A modular service layout keeps customer requests, merchant operations, and background work easy to reason about.</p>
          </div>
          <ArchitectureDiagram />
        </section>

        <section id="principles" className="border-t border-[#e5d9c3] bg-[#2a332b] text-[#fffaf0]">
          <div className="mx-auto grid max-w-7xl gap-8 px-5 py-14 sm:px-8 sm:py-16 md:grid-cols-[0.8fr_1.2fr] md:items-center">
            <div><p className="font-sans text-xs font-semibold uppercase tracking-[0.08em] text-[#b5d0ad]">Built around the hard parts</p><h2 className="mt-3 max-w-sm font-sans text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">Reliability is a product feature.</h2></div>
            <div className="grid gap-5 sm:grid-cols-3">
              {[
                ['Isolation', 'Tenant context is carried through requests and data access.'],
                ['Recovery', 'Checkout steps can retry, compensate, and reconcile.'],
                ['Visibility', 'Structured events make operations easier to inspect.'],
              ].map(([title, copy]) => <div key={title} className="border-l border-white/20 pl-4"><h3 className="font-sans text-base font-semibold">{title}</h3><p className="mt-2 text-[13px] leading-relaxed text-white/75">{copy}</p></div>)}
            </div>
          </div>
        </section>
      </main>
      <footer className="flex flex-col items-start justify-between gap-3 px-5 py-6 text-xs text-[#74695c] sm:flex-row sm:items-center sm:px-8">
        <span>© {new Date().getFullYear()} ScaleForge</span><a href="#top" className="underline decoration-[#c8baa2] underline-offset-4 hover:text-[#1f6d4c]">Back to top</a>
      </footer>
    </div>
  )
}

function SignInAction({ label = 'Sign in' }: { label?: string }) {
  return <Button size="lg" className="h-10 rounded-lg px-5 font-body font-semibold" style={{ backgroundColor: theme.emerald, color: theme.ivory }} onClick={() => navigateToLogin('/')}>{label}<ArrowRight size={15} /></Button>
}

export function SignInPrompt() {
  const session = useSession()

  if (session.isLoading) return null

  if (session.data?.authenticated) {
    const firstOrg = session.data.memberships?.[0]
    if (firstOrg) return <Navigate to={`/console/orgs/${firstOrg.org_id}/members`} replace />
    if ((session.data.platform_roles?.length ?? 0) > 0) return <Navigate to="/admin/orgs" replace />

    // A shopper without staff memberships can still reach their order history.
    return <LandingPage action={<Button asChild size="lg" className="h-10 rounded-lg px-5 font-body font-semibold" style={{ backgroundColor: theme.emerald, color: theme.ivory }}><Link to="/account/orders">View my orders <ArrowRight size={15} /></Link></Button>} />
  }

  return <LandingPage action={<SignInAction />} />
}
