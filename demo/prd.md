# PRD: Public API Rate Limiting

**Status:** Draft for review · **Owner:** Platform team

## Problem

Third-party integrations occasionally hammer our public API, degrading latency for
everyone. We need per-customer rate limiting before the next partner launch.

## Goal

Cap each API key at a fair request rate, reject excess with `429 Too Many Requests`,
and keep added latency under 2 ms per request.

## Proposed design

- **Limit:** 1000 requests/minute per API key.
- **Algorithm:** fixed-window counter. For each request, `INCR` a Redis key
  `rl:{api_key}:{current_minute}` and set `EXPIRE 60` on it; if the value exceeds 1000,
  return `429`.
- **Scope:** one global limit per key (all endpoints share the same budget).
- **Storage:** a single Redis instance shared across API nodes.

## Open questions for reviewers

1. Is the fixed-window algorithm correct under bursty traffic?
2. Are there failure modes in the Redis counter approach?
3. Is one global per-key limit enough for v1, or do we need per-endpoint limits?

## Out of scope (v1)

Per-endpoint limits, dynamic per-plan limits, and multi-region replication.
