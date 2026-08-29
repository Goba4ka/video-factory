# Human medical review — fail closed

Review every claim for the `health` and `chinese_medicine` lanes before scripting.

This production gate is completed only by an attributable qualified human. The
autonomous agent may prepare evidence, but must never claim or complete the
`medical_review` task. Record the reviewer's identity, qualification, timestamp,
decision note, and the exact reviewed artifact binding.

Required output: a valid `safety_gate_report` with `gate_type=medical_safety`.

- Separate established evidence, preliminary evidence, traditional use and unsupported claims.
- Do not diagnose, prescribe, choose a dose, promise a cure, or tell viewers to stop treatment.
- Record contraindications, interactions, red flags and populations needing clinician advice.
- Every factual medical claim must map to a dated authoritative or peer-reviewed source.
- `passed=false` when evidence is missing, contradictory, outdated, or stronger than the wording.
- `needs_human_review=true` for dosage, pregnancy, children, serious symptoms, treatment substitution, or ambiguous risk.
- A blocking finding prevents script, render and publication tasks from becoming eligible.
