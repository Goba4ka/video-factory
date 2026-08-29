# V2 failure lifecycle

This document is the operational contract for failures, dead letters, retries,
rework, and artifact invalidation in the video-factory control plane.

## Invariants

1. A task enters `dead` only after an explicit terminal failure, exhausted attempt
   budget, terminal lease expiry, a dead dependency, or replacement by a controlled
   rework.
2. Every transition into `dead` appends one immutable `dead_letters` cycle. A cycle
   contains the normalized error and a full task snapshot at death time.
3. A dead-letter cycle stays `open` until an attributable operator action resolves
   it. Resolution records actor, reason, action, timestamp, and idempotency key.
4. `dead-retry` is for transient failures only. It does not change payload,
   dependency, role, pod, or kind; it preserves all attempt rows and adds a bounded
   attempt allowance (maximum 100 total attempts).
5. A task whose dependency is still not `succeeded` cannot be retried directly.
   `--cascade-dependents` revives only descendants whose latest failure code is
   `dependency_dead`; independently failed descendants remain dead.
6. `task-rework` is for corrected inputs. It refuses a subtree with active leases,
   clones the selected task and every downstream task with fresh task IDs and zero
   attempts, shallow-merges the supplied JSON patch into the new root payload, and
   rewires every cloned dependency to the matching replacement.
7. Old attempts and successful task results are immutable. Old queued tasks replaced
   by rework become resolved `rework_superseded` dead-letter cycles. The
   `task_reworks` ledger stores the complete old-to-new mapping.
   A `publisher` task cannot be the root of rework: remote outcome must be reconciled
   first, then the same immutable publication may use `dead-retry`. Upstream reworks
   still clone the downstream publisher behind a fresh final-review gate.
8. Artifact identity consists of payload checksum, exact dependency artifact IDs and
   checksums, producer, producer version, prompt version, model, and arbitrary JSON
   metadata. A change to any identity-bearing field creates a new artifact version.
9. Replacing an active artifact supersedes that version and transitively marks all
   active dependent artifacts `invalidated`. Unrelated jobs are not affected.
10. All mutating queue commands use `BEGIN IMMEDIATE`, fenced leases where relevant,
    and idempotency keys. Reusing a key with different inputs fails closed.

## Operator commands

All examples assume `VIDEO_FACTORY_DB` is set, or add `--db /path/factory.sqlite3`.

Inspect the open DLQ:

```bash
video-factory dead-list --status open
video-factory dead-list --task-id task_abc --status all
```

Retry unchanged inputs after a transient incident:

```bash
video-factory dead-retry task_abc \
  --actor oncall@example.com \
  --reason "provider incident resolved" \
  --additional-attempts 1 \
  --cascade-dependents \
  --idempotency-key incident-20260829-task-abc
```

Create an input-corrected rework run:

```bash
video-factory task-rework task_abc \
  --actor editor@example.com \
  --reason "source snapshot changed" \
  --payload-patch corrected-input.json \
  --idempotency-key rework-20260829-task-abc-v2
```

Register an artifact. `dependencies.json` is either an array of artifact records or
an object with a `dependencies` array. `metadata.json` must be an object.

```bash
video-factory artifact-put \
  --root /var/lib/video-factory/artifacts \
  --job-id job_abc --kind script --file SCRIPT.json \
  --producer script-agent --producer-version 2.1.0 \
  --prompt-version script-v7 --model gpt-5 \
  --dependencies dependencies.json --metadata metadata.json

video-factory artifact-current \
  --root /var/lib/video-factory/artifacts --job-id job_abc --kind script

video-factory artifact-list \
  --root /var/lib/video-factory/artifacts --job-id job_abc --status invalidated
```

`artifact-put` returns `invalidated`, the exact versions whose status changed during
that operation. Workers must never consume a record unless its status is `active`.

## Decision table

| Situation | Action | Payload changes | Attempt history | Downstream |
|---|---|---:|---:|---|
| Network/provider interruption | `dead-retry` | No | Preserved | Optional dependency-only revival |
| Worker crash before lease ACK | `recover-expired` | No | Expired attempt preserved | None until budget exhausted |
| Bad source, claim, script, edit input | `task-rework` | Yes | Old history preserved; new tasks start at 0 | Entire subtree cloned and rewired |
| Upstream artifact bytes changed | `artifact-put` | New checksum | N/A | Transitive invalidation |
| Producer/prompt/model/source metadata changed | `artifact-put` | Same bytes allowed | N/A | Transitive invalidation |
| Parent still dead | Fix/retry/rework parent first | N/A | N/A | Child retry fails closed |

## Monitoring and recovery

- Alert when `queue-status.open_dead_letters > 0` for longer than the lane SLA.
- Alert on repeated DLQ cycles for the same task, not merely the aggregate dead count.
- Back up the SQLite database with the online `.backup` command; the DLQ and rework
  ledgers are in the same transactional database.
- Back up the complete artifact root, including `index.json`; artifact payload files
  alone are insufficient to reconstruct active/invalidated pointers.
- Never edit task rows, the artifact index, or immutable artifact payloads manually.
  Use the commands above so lifecycle evidence remains attributable and replay-safe.

## Acceptance checks

```bash
python -m pytest \
  factory/tests/test_failure_lifecycle.py \
  factory/tests/test_artifact_store.py \
  factory/tests/test_artifact_cli.py \
  factory/tests/test_queue.py \
  factory/tests/test_queue_cli.py -q
```

The suite covers terminal failure, attempt exhaustion, lease recovery, dependency
propagation, safe retry, cascade restrictions, rework DAG rewiring, lease fencing,
idempotency replay, metadata/checksum invalidation, and the public CLI surface.
