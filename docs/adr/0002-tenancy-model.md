# ADR-0002: Tenancy model

- **Status:** Accepted (2026-10-17 — OQ-1 confirmed using the default in this decision)
- **Date:** 2026-10-17

## Context

The PRD describes a "multi-tenant commerce platform" with organization-scoped admin roles (catalog, inventory, and order managers) and customers who "manage own cart and access own orders". It does not say whether customers belong to organizations, or what a tenant owns. Every data model depends on this.

## Decision

- **An organization is a merchant tenant that operates one store.**
  - Org-scoped data: products, SKUs, categories, prices, promotions, stock, reservations, orders, payments, store settings, and staff memberships. Each carries `org_id`.
  - Customers are **platform-level accounts**. A customer can buy from any `active` store without being a member of it.
- **Scoping of customer-owned data:**
  - A cart is scoped per (customer, org).
  - An order belongs to one org and one customer.
- **Who sees an order:**
  - Customers see their orders across stores.
  - Staff see only their org's orders.
- **Storefront URL:** `/s/{storeSlug}` for browsing. The APIs use the org UUID.
- **Platform superusers** operate across tenants only through platform endpoints.

## Alternatives

| Option | Why not (for now) |
| --- | --- |
| Single-store platform; orgs are only "teams" for admins | Weakens the multi-tenancy showcase. Tenant isolation would only cover admin data. |
| Customers belong to orgs (B2B: each org buys) | Different product (procurement). Complicates the customer role model. |
| Marketplace with a multi-store cart | Checkout spans several tenants' inventory and payments (split orders, split payments). High complexity for little learning value beyond the saga already present. |

## Consequences

- Every org-scoped table leads its indexes with `org_id`. Every query filters on it (LLD-09 §3).
- Customer-facing read APIs (storefront) are public or customer-domain. Admin APIs are org-domain (LLD-02).
- Cross-store purchasing needs one checkout per store.

## Revisit triggers

- The product owner chooses B2B or a marketplace.
- A multi-store cart becomes a requirement.
