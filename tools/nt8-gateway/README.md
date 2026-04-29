# NT8 Gateway Scaffold

This directory is the repo-local scaffold for the future NinjaTrader 8 AddOn.
It is intentionally separate from the Python research/runtime process.

Scope:

- Local-only transport: HTTP/WebSocket or named pipe.
- Sim account only until external validation passes.
- Append-only event stream for heartbeat, account snapshots, instrument discovery, order updates, incidents, and external interventions.
- Required protocol fields: `schema_version`, `protocol_version`, `correlation_id`, `idempotency_key`, and monotonic `sequence`.
- Supported commands: `marketOrder`, `marketBatch`, `cancelOrders`, `flatten`, `flattenBatch`, `closeQty`, and `bracket`.

The C# files here are a compile-ready project skeleton, not a completed production NT8 AddOn. Production use still requires Windows, NinjaTrader 8 SDK references, sim account validation, disconnect/reconnect drills, and 5 trading days or 200 sim commands with zero unexplained duplicate submissions.
