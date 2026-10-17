# ADR-0003: Message broker — RabbitMQ with transactional outbox

- **Status:** Accepted (2026-10-17)
- **Date:** 2026-10-17

## Context

PRD §6.1 asks for one broker, choosing between RabbitMQ (task queues, routing, retries, dead-lettering) and Kafka (durable log, replay, partitions). The workflows the PRD names are:

- checkout saga commands and events;
- compensation (release, void, refund);
- notification jobs;
- audit fan-out;
- search indexing;
- policy-change propagation.

The PRD's explicit requirements are bounded retries with backoff, dead-lettering with inspection/replay, duplicate tolerance, and the outbox.

## Decision

- **Broker:** RabbitMQ 4.x, a 3-node cluster run by the RabbitMQ Cluster Operator on Kubernetes (single node in Compose).
- **Queues:** quorum queues for all durable consumers.
- **Exchanges:**
  - One durable topic exchange `sf.events`, with routing key = `<event type>.v<major>`.
  - Per-consumer retry exchanges and queues, and a DLQ.
- **Publishing:** only through a **transactional outbox** in the producer's schema. The outbox is retained for 7 days after publish, so it doubles as the replay source.
- **Replay:**
  - From the DLQ (per consumer).
  - From the outbox (per producer, by time range or aggregate) when broker data is lost.

## Alternatives

| Option | Pros | Cons |
| --- | --- | --- |
| Kafka (KRaft) | Native replay, partitions give per-key ordering, consumer groups | No native per-message delay/retry/DLQ (needs retry-topic patterns). Heavier operations. Replay is less needed because the outbox already provides it. |
| Redis Streams | Already deployed | Weaker durability story. Would couple messaging to the cache/session failure domain. |
| PostgreSQL-only queue (SKIP LOCKED) | No new infrastructure | Does not demonstrate pub/sub or broker routing. Adds load to the primary DB. |

RabbitMQ fits the task-queue, retry, and DLQ-heavy workflows. The outbox covers the "replay" learning goal at producer level. RabbitMQ Streams remain available if log-style replay is wanted later.

## Consequences

- Ordering is per queue, not per aggregate key. Consumers must tolerate reordering: version checks and state-machine guards (LLD-08 §9).
- Delay tiers are fixed-TTL queues, so jitter is coarse (LLD-08 §7).
- The broker is not the system of record. Broker loss means republishing from the outbox.

## Revisit triggers

- A need for multi-consumer historical replay beyond 7 days.
- Analytics or event-sourcing use cases.
- Sustained throughput beyond what a 3-node RabbitMQ cluster handles in load tests.
