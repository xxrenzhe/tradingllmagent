# Backend Rollout Gates

## Sequence

1. Contract tests pass for OpenAPI execution and gateway schemas.
2. Paper shadow runs at least 10 trading days with replay-consistent intents.
3. NT8 sim gateway runs at least 5 trading days or 200 sim commands with zero unexplained reconciliation drift.
4. Micro-live requires external NT8 validation, manual approval audit, and broker-side protection.
5. Controlled live requires external broker validation, strategy profile whitelist, kill switch verification, and drift-free samples.

## Default Failure Behavior

- Missing evidence blocks promotion.
- Any unresolved incident blocks promotion.
- Any high-risk drift downgrades the profile to `paper_shadow`.
