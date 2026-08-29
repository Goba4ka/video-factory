# Views feedback loop

Views cannot be guaranteed. The factory optimizes the probability of reach by
learning from comparable posts while protecting originality, rights, and
factual quality.

## Snapshot schedule

Collect platform metrics at 1, 6, 24, 72, and 168 hours. Use engaged views,
retention, completion, shares, saves, and follows whenever the platform exposes
them. Raw view counts are secondary because definitions and distribution vary
by platform.

## Comparable cohorts

Judge a video only against posts from the same account, platform, topic pod,
duration band, and first 72-hour age. A global absolute threshold is not a
useful success criterion for a new account.

Baseline windows:

- Early calibration: latest 10 comparable posts.
- Stable operation: latest 30-50 comparable posts, maximum age 90 days.
- Recalculate after a material platform or channel-positioning change.

## Decision signals

- `hook`: stayed-to-watch or the earliest available retention point.
- `hold`: average percentage viewed and completion rate.
- `value`: shares + saves per 1,000 engaged views.
- `conversion`: follows per 1,000 engaged views.
- `safety`: recommendation restrictions, claims, removals, negative feedback.
- `efficiency`: production minutes and cost per approved render.

A winner must exceed the cohort median on `hook` and at least one of `hold`,
`value`, or `conversion`, with no safety event. One viral outlier never rewrites
the topic pack by itself.

## Daily exploration budget

- 60% proven topic clusters and hook archetypes.
- 30% adjacent topics or one controlled creative change.
- 10% tail experiments with a genuinely different format or visual silhouette.

Change only one major dimension per test: topic, hook, duration, caption style,
voice, shot density, or ending. This preserves a usable causal signal.

## Update rules

1. Wait for the 72-hour snapshot before changing a topic-pack weight.
2. Require at least five comparable outputs before promoting an archetype.
3. A winner may spawn at most two follow-ups, each with a new fact and angle.
4. Do not copy the winning script, opening sentence, asset sequence, or final
   silhouette.
5. Policy or rights incidents always reduce the relevant source/archetype
   weight, regardless of views.
6. Human editors approve topic-pack changes. Metrics do not change factual
   confidence or license decisions.

## Initial north-star

Until every pod has at least 30 posts, optimize for a balanced score:

`0.35 hook + 0.30 hold + 0.20 value + 0.15 conversion`

Each component is its percentile rank inside the comparable cohort. Report the
four components alongside the score so a high number cannot hide weak viewer
retention.
