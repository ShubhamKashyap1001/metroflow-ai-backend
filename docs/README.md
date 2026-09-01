# MetroFlow backend — engineering docs

This folder documents how the major backend subsystems work and the
real bugs that were found and fixed while building them. Each file
covers one feature area end-to-end: what it does, how it's
implemented, and — where relevant — what was broken before and how it
was diagnosed and fixed. There is no chronological "phase" numbering
here; everything is organized by subsystem so you can go straight to
the part of the system you're working on.

| Doc | Covers |
|---|---|
| [architecture-audit.md](./architecture-audit.md) | Full-codebase audit findings (severity-ranked) that the fixes below were scoped from |
| [ai-recommendations.md](./ai-recommendations.md) | The AI Insights panel, the recommendations endpoint, and the request/write-storm fix |
| [crowd-live-state-and-retention.md](./crowd-live-state-and-retention.md) | Live crowd state vs. historical crowd log, retention/rollup, check-in/check-out concurrency |
| [crowd-data-correctness.md](./crowd-data-correctness.md) | What the dataset's columns actually mean, and how occupancy/capacity/crowd-level/AI-threshold math was derived from real data |
| [realtime-websocket-system.md](./realtime-websocket-system.md) | `/ws/monitor`: heartbeat, cross-process Redis relay, de-duplication, coalescing, stale-connection reaping, reconnect resync, backgrounded-tab stability |
| [background-jobs-and-leader-election.md](./background-jobs-and-leader-election.md) | How the crowd simulator, train tracker, and retention job run exactly once across multiple worker processes |
| [database-sessions-and-connection-pooling.md](./database-sessions-and-connection-pooling.md) | SQLAlchemy session lifecycle, connection-pool sizing, statement timeouts, worker×pool capacity checks |
| [notification-delivery.md](./notification-delivery.md) | Email/SMS dispatch: its own thread pool, duplicate-notification prevention, and transient-failure retry |
| [query-performance-and-indexing.md](./query-performance-and-indexing.md) | N+1 query elimination, indexes added for alerts/notifications, and the schedule/crowd query fixes found by ranking real query execution time |
| [rate-limiting.md](./rate-limiting.md) | Tiered rate limits and making them work across multiple workers |
| [passenger-analytics-model-selection.md](./passenger-analytics-model-selection.md) | Why the crowd-prediction model under-predicted every city except Delhi/Kolkata, and the model-selection fix |

Most docs also describe how each fix was verified (real Postgres/Redis
where available, dependency-free harnesses otherwise) — that detail is
kept because it explains *why* a given fix can be trusted, not as a
changelog.
