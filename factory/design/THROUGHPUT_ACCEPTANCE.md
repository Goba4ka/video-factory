# Production throughput acceptance

`throughput-acceptance` is the fail-closed production-evidence gate for a
completed 10-15 video batch. It does not estimate throughput from synthetic
tasks. It proves that the requested number of real, checksum-bound QC masters
was produced inside a deadline.

The command is deliberately read-only:

- SQLite is opened with `mode=ro` and `PRAGMA query_only=ON`;
- the gate never initializes or migrates a database;
- it does not call providers, renderers, TTS, reviewers, or publishers;
- it never completes `final_review` and never invokes `publisher`.

## Command

Run from the repository root:

```powershell
$env:PYTHONPATH = (Resolve-Path 'factory/src').Path
python -m video_factory throughput-acceptance `
  --db D:\video-factory\runtime\factory.sqlite3 `
  --registry factory/lanes/registry.json `
  --target 15 `
  --deadline-hours 24 `
  --batch-id batch_2026_08_30 `
  --safety-margin 0.20 `
  --gpu-heavy-slots 1 `
  --evidence-root D:\video-factory\runtime `
  --export D:\video-factory\reports\throughput-acceptance.json
```

`--batch-id` is optional. If omitted, the command audits the latest batch by
job creation time. `--target` must be from 10 through 15.

`--safety-margin` reserves part of the deadline for variance. With a 24-hour
deadline and a `0.20` margin, the accepted batch wall clock must be at most
19.2 hours. `--gpu-heavy-slots` describes the number of real shared heavy
execution slots configured on the audited host; the default is one.

`--evidence-root` is repeatable. Every master, receipt, contact sheet, and QC
evidence file must resolve inside one of these trusted roots and must not itself
be a symlink. If omitted, only the database directory is trusted.

The command exits with code `0` only when the gate accepts the batch. A
well-formed but rejected batch exits with code `3`. Input, schema, and operating
system errors exit with code `2`.

## Evidence requirements

Acceptance requires all of the following:

1. The database schema exactly matches the current control-plane schema.
2. The batch contains exactly the requested target.
3. All five enabled lanes are present in the registry allocation. The gate
   reproduces the registry's minimum-first, registry-order distribution.
4. Every job has the exact registry DAG and role order for its lane.
5. Every stage through `qc` is `succeeded`, has a durable successful attempt,
   and has valid claim/finish timing.
6. There are no active leases, unresolved expired work, dead tasks, or open
   dead-letter records.
7. `final_review` and `publisher` remain queued, pristine, and unattempted.
8. Simulation and shadow markers are absent. `simulate-day`, `shadow_soak`,
   and payload/result markers such as `simulation_run` are never production
   evidence.
9. Every accepted job has a `RenderManifest` whose SHA-256 matches the actual
   master bytes.
10. Caption transcript, automatic QC, five semantic analyzers, the
    eight-category evidence bundle, contact sheet, and final QC report all
    validate and bind to the same job, lane, render ID, master checksum, and
    evidence bytes.
11. The number of checksum-verified QC masters reaches the target within the
    deadline after the configured safety margin.
12. Measured shared `gpu-heavy` handler time fits the configured slot capacity.

Any missing or stale evidence rejects the batch. A pre-existing MP4, an empty
queue simulation, or a manually written report cannot close this gate by
itself.

## Report fields

The JSON report includes:

- expected and actual lane distribution;
- accepted QC master count and per-job checksum summaries;
- batch start, last QC completion, and wall-clock time;
- queue-dwell and handler-duration p50/p95/max overall, per role, per lane, and
  per role/lane;
- shared `gpu-heavy` busy time, capacity, and utilization proxies;
- open DLQ, expired lease, and resolved retry counts;
- structured fail-closed error codes.

`throughput_accepted` is the result of this one-batch gate.
`production_ready` intentionally remains `false`: server, provider, rights,
observer-model, review, and publication cutover are separate gates. The report
also records observed final-review and publisher status counts and states that
this read-only command performed no gate actions.

The GPU figure is a conservative scheduling proxy derived from successful
attempt durations for roles protected by the shared heavy-resource lock. It is
not a substitute for host GPU telemetry.

## Server cutover use

Run this command only after a real mixed-lane batch has completed QC on the
target runtime host. Store the exported report beside the immutable batch
artifacts and host-acceptance evidence. A passing report closes the measured
batch-throughput item only; it does not approve publication and does not erase
other blockers in the V2 acceptance document.
