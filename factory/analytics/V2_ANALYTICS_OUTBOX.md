# V2 analytics and publish outbox

This layer makes production telemetry and post-publication feedback durable while
keeping publication fail closed. It never calls TikTok, Instagram, YouTube, or any
other external service.

## Invariants

- `outbox-create` accepts only a `ready` job whose rights and QC gates passed.
- The supplied render SHA-256 is checked against the actual file before enqueue,
  approval, connector claim, and successful completion.
- Creating an outbox item does not approve it. `outbox-approve` is a separate
  explicit human action binding both `render_sha256` and `metadata_sha256`.
- A connector receives a fenced lease and stable
  `delivery_idempotency_key`. Only the current, unexpired lease token can record a
  result.
- An expired or ambiguous delivery becomes `unknown`, never automatically retries.
  A human must reconcile it with the platform before another publication attempt.
- Feedback imports require a `published` outbox record and must match its job,
  platform, remote ID, and render SHA-256. A bundle is imported transactionally:
  one invalid snapshot rolls back the entire file.
- Every mutating CLI command requires a globally unique `--idempotency-key`.
  Reusing a key with different input is rejected.
- Metadata, receipts, errors, and metrics reject fields that look like credentials.

## Database tables

- `production_metrics`: immutable, event-keyed stage duration/cost/resource data.
- `publish_outbox`: one exact render plus one exact destination and its approval,
  lease, receipt, or terminal failure state.
- `performance_feedback`: immutable platform snapshots attributed to a published
  outbox checksum.
- `performance_feedback_dimensions`: lane, duration band, and canonical snapshot
  window used to prevent cross-cohort comparisons.
- `editorial_recommendations`: immutable evaluations and bounded editorial-only
  recommendations; these records cannot modify jobs, rights, or factual gates.

Schema version is `6`. `video-factory init --db ...` performs the additive migration.

## Production metrics

Agents may record provider/resource data directly:

```json
{
  "schema_version": "1.0.0",
  "event_id": "evt-health-render-20260829-001",
  "job_id": "job_health_001",
  "lane": "health",
  "stage": "render",
  "status": "succeeded",
  "occurred_at": "2026-08-29T08:01:00Z",
  "duration_seconds": 42.5,
  "attempts": 1,
  "estimated_cost_usd": 0.12,
  "gpu_seconds": 40,
  "output_bytes": 12345678,
  "metadata": {"profile": "high"}
}
```

```bash
video-factory metrics-record metric.json \
  --db /var/lib/video-factory/factory.sqlite3 \
  --idempotency-key metric:evt-health-render-20260829-001
```

The timer-friendly queue collector derives duration and status from completed task
attempts. Use a different time-bucket key for each run:

```bash
video-factory metrics-collect-queue \
  --db /var/lib/video-factory/factory.sqlite3 \
  --idempotency-key "metrics-collect:$(date -u +%Y%m%dT%H%M)"

video-factory analytics-summary \
  --db /var/lib/video-factory/factory.sqlite3 \
  --since 2026-08-29T00:00:00Z
```

## Publish handoff

Create `publish-request.json` with one destination:

```json
{
  "schema_version": "1.0.0",
  "job_id": "job_health_001",
  "render_id": "render_health_001",
  "render_path": "/srv/video-factory/releases/current/final.mp4",
  "render_sha256": "<64 lowercase hex characters>",
  "qc_report": "qc/final.json",
  "destination": {
    "platform": "tiktok",
    "account_id": "health-ru",
    "caption": "Проверяем популярный миф",
    "visibility": "draft"
  },
  "disclosures": {
    "ai_generated": false,
    "altered_or_synthetic": false,
    "paid_promotion": false,
    "notes": []
  }
}
```

```bash
video-factory outbox-create publish-request.json \
  --db /var/lib/video-factory/factory.sqlite3 \
  --idempotency-key outbox:create:job_health_001:tiktok

# A human copies both hashes from the pending item after reviewing the exact file.
video-factory outbox-approve out_... \
  --render-sha256 ... \
  --metadata-sha256 ... \
  --approved-by owner@example.com \
  --approval-note "Reviewed exact render, caption, account and disclosures" \
  --human-confirm I_REVIEWED_THIS_RENDER \
  --db /var/lib/video-factory/factory.sqlite3 \
  --idempotency-key outbox:approve:out_...:v1
```

The future connector uses `outbox-claim`, performs the external request itself, and
then records a receipt through `outbox-complete` or a terminal/ambiguous outcome
through `outbox-fail`. The control plane itself performs no send:

```bash
video-factory outbox-claim --worker tiktok-connector-01 --platform tiktok \
  --lease-seconds 900 --db /var/lib/video-factory/factory.sqlite3 \
  --idempotency-key outbox:claim:tiktok:20260829T0815

video-factory outbox-recover-expired \
  --db /var/lib/video-factory/factory.sqlite3 \
  --idempotency-key outbox:recover:20260829T0830
```

Do not put a lease token on a shared command line in a multi-user host. A connector
service should invoke the Python API directly or load the token from a root-owned
runtime file/stdin. CLI flags are intended for controlled testing and recovery.

## Performance feedback

The import bundle wraps the existing canonical `metrics_snapshot` contract:

```json
{
  "schema_version": "1.0.0",
  "snapshots": [
    {
      "outbox_id": "out_...",
      "render_sha256": "<published render checksum>",
      "cohort": {
        "lane": "health",
        "duration_seconds": 28.0
      },
      "snapshot": {
        "schema_version": "1.0.0",
        "job_id": "job_health_001",
        "platform": "tiktok",
        "remote_id": "platform-post-id",
        "captured_at": "2026-08-29T09:00:00Z",
        "age_hours": 1,
        "metrics": {
          "views": 1000,
          "engaged_views": 800,
          "stayed_to_watch_rate": 0.71,
          "average_view_duration_seconds": 17.4,
          "average_percentage_viewed": 0.68,
          "completion_rate": 0.52,
          "likes": 90,
          "comments": 12,
          "shares": 22,
          "saves": 18,
          "follows": 7,
          "negative_feedback": 0
        },
        "policy_events": []
      }
    }
  ]
}
```

```bash
video-factory feedback-import feedback.json \
  --db /var/lib/video-factory/factory.sqlite3 \
  --idempotency-key feedback:tiktok:platform-post-id:1h

video-factory feedback-list --platform tiktok \
  --db /var/lib/video-factory/factory.sqlite3
```

Suggested collection windows remain 1, 6, 24, 72, and 168 hours. Performance data
may tune editorial experiments; it never overrides factual, medical, rights, or
human publication gates.

## Bounded editorial feedback

`feedback-evaluate` chooses the candidate's 72-hour snapshot. If that snapshot is
not available, it uses the nearest available canonical window from 1, 6, 24, or 168
hours and labels the result `nearest_canonical`. It then builds a cohort using only:

- the same lane;
- the same platform and account;
- the same duration band (`under_20`, `20_34`, `35_59`, or `60_plus`);
- the same canonical snapshot window;
- the latest snapshot per other outbox, captured no more than 90 days earlier.

At least five usable comparable outputs are mandatory. A missing cohort returns
`insufficient_cohort`; it never promotes a winner. A policy event returns
`safety_blocked` even when reach signals are high.

```bash
video-factory feedback-evaluate out_... \
  --minimum-cohort 5 \
  --db /var/lib/video-factory/factory.sqlite3 \
  --idempotency-key feedback-evaluate:out_...:72h:v1

video-factory recommendations-list --outbox-id out_... \
  --db /var/lib/video-factory/factory.sqlite3
```

Recommendations are restricted to `hook`, `hold`, `value`, and `conversion`. A
winner may recommend at most two new controlled angles with new facts, wording, and
footage. A nonwinner may receive at most two test hypotheses but gets zero cloned
follow-ups. The evaluator performs no job mutation and no publication, makes no
reach guarantee, and explicitly preserves factual confidence, rights confidence,
medical safety, and human publish approval.
