# ADR-0007: Search backend — PostgreSQL full-text first

- **Status:** Proposed
- **Date:** 2026-10-17

## Context

PRD §13.1 sets the direction "PostgreSQL search first; evaluate OpenSearch only if justified by measured needs". FR-COM-04 requires documented search source and index freshness.

## Decision

- search-svc owns a **derived** table `search.product_search_docs`, with:
  - a weighted `tsvector` (name A, category B, description C);
  - `pg_trgm` trigram indexes for prefix and typo tolerance;
  - filter columns.
- The table is fed by catalog and inventory events (event-carried state transfer), and rebuilt by a reindex job that reads the catalog export API.
- Queries use `websearch_to_tsquery` for parsing, `ts_rank_cd` for ranking, and keyset pagination for non-relevance sorts.

## Evaluation criteria for moving to OpenSearch

Move only if at least one holds in M4 load tests:

- search p95 > 300 ms at target load after index tuning;
- relevance needs (synonyms, facets with counts at scale, language analyzers) that PostgreSQL cannot meet acceptably;
- search load measurably degrades T0 database work even after moving it to a replica.

## Consequences

- No new infrastructure product.
- The search schema can move to a replica or its own cluster under ADR-0006's criteria.
- Facet counts are computed by aggregate queries, which may be costly at scale. This is measured in M4.
