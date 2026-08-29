# Headless worker runtime

`video-factory worker` is the queue consumer for deterministic server roles and
future editorial-agent backends. It does not publish externally and does not
execute commands found inside task payloads.

## Safety invariants

1. A configured advisory resource lock is acquired **before** `claim` and held
   until the attempt is acknowledged. `auto` maps render and QC to one shared
   `gpu-heavy.lock`; other roles can be given an explicit lock path or `none`.
2. Every attempt receives a random fencing token from SQLite. The handler never
   sees that token. Completion, failure, and heartbeat are accepted only for the
   current token and lease owner.
3. `renew-lease` extends both the task and its active attempt. A heartbeat never
   shortens a lease and does not grow the idempotency operations table.
4. Complete/fail use an acknowledgement key derived from task ID plus a hash of
   the fencing token. Ambiguous SQLite/I/O errors are retried with exactly that
   key, so a committed acknowledgement is replayed instead of duplicated.
5. SIGINT/SIGTERM stops new claims. The active handler may drain for the configured
   grace period while heartbeat continues; it is terminated only after that bound.
6. Handler argv, runtime, stdin/stdout/stderr bytes, acknowledgement retries,
   polling, idle polls, completed tasks, and total worker runtime all have
   explicit bounds.

Empty queue polls are not written to `operations`; a successful claim is. This
preserves ambiguous-claim replay without adding tens of thousands of rows per idle
worker each day.

## Python executor contract

An agent backend registers an exact task kind or a `*` fallback:

```python
from video_factory.worker import ExecutorRegistry, HeadlessWorker

def executor(task: dict, stop_event) -> dict:
    # task contains id/job_id/role/pod/kind/payload/attempt metadata and a
    # root-first upstream_results list for every succeeded dependency.
    # It never contains lease_token.
    return {"artifact": {...}}

registry = ExecutorRegistry()
registry.register("research.claim_ledger", executor)
```

The callable signature is:

```text
executor(task_public: Mapping[str, Any], stop_event: threading.Event) -> dict
```

Return the exact queue result object. Raise `ExecutorError(code, message,
retryable=True)` for an expected failure. Other exceptions are treated as
retryable executor failures. Contract/gate validation remains authoritative in
`Dispatcher.complete`.

`Dispatcher.execution_context(...)` is read-only and fenced by the current task
lease. It returns only task/result metadata for a same-job, same-pod, entirely
succeeded transitive dependency chain, ordered from the root dependency to the
immediate parent. Unknown, unfinished, cross-boundary, malformed, cyclic, or
credential-shaped upstream data blocks execution. Upstream task payloads,
attempts, owners, and lease tokens are never returned.

## Subprocess handler contract

The CLI adapter starts one trusted executable with `shell=False`, writes one UTF-8
JSON task object to stdin, and expects exactly one UTF-8 JSON object on stdout.
Non-zero exit, invalid JSON, oversized output, timeout, and shutdown-grace expiry
become bounded task failures. Handler stderr content is not copied into queue JSON.

One-task acceptance example:

```bash
video-factory worker \
  --worker render-01 \
  --role render \
  --pod health \
  --handler-executable /opt/video-factory/bin/render-handler \
  --resource-lock auto \
  --lease-seconds 900 \
  --heartbeat-seconds 120 \
  --max-tasks 1 \
  --db "$VIDEO_FACTORY_DB"
```

Long-running systemd mode uses `--max-tasks 0 --max-idle-polls 0`; set
`TimeoutStopSec` greater than `--shutdown-grace`. For a soak test, use finite
`--max-tasks`, `--max-idle-polls`, or `--max-runtime-seconds`.

Manual heartbeat diagnosis:

```bash
video-factory renew-lease TASK_ID \
  --worker render-01 \
  --lease-token "$LEASE_TOKEN" \
  --lease-seconds 900 \
  --db "$VIDEO_FACTORY_DB"
```

Never place lease tokens or provider secrets in service arguments, logs, handler
stdout, artifacts, or task payloads. The manual command is diagnostic only; the
worker heartbeats internally.
