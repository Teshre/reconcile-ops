# Reconcile-Ops — Post-mortem / Engineering Retrospective

A candid write-up of building Reconcile-Ops: what held up, what didn't, the
trade-offs I made on purpose, and where I'd take it next. Written for a reader
evaluating how I think about a problem, not just the finished artifact.

---

## Context

**Goal:** build a PSP-vs-ledger payments reconciliation pipeline that classifies
every discrepancy into an actionable exception type and quantifies money at risk,
end-to-end (data → reconciler → KPIs → CLI + dashboard), with honest accuracy
metrics.

**Constraints I set:** minimal dependencies (all with Python 3.14 wheels),
zero-setup SQL over CSVs (DuckDB), fully reproducible synthetic data, and a
schema contract shared verbatim across every component.

---

## What worked

- **A single, explicit data contract.** Fixing the exact column names and the
  `reference == payment_ref` matching key up front — and documenting the amount
  semantics (`expected_amount` is *gross*, fee explains gross→net) — meant the
  generator, reconciler, SQL model, KPI layer, and dashboard never disagreed
  about the shape of the data.
- **Synthetic data with ground truth.** Injecting each break type at a known
  rate and recording the correct label per row turned "does the matcher work?"
  from a vibe into a number (precision/recall). This is the single highest-value
  decision in the project.
- **Making `fee_mismatch` and `amount_mismatch` separable.** Splitting *gross
  agreement* from *fee/net agreement* meant these two collapse into distinct,
  100%-separable buckets instead of one ambiguous "amounts are off" pile.
- **Determinism.** A fully seeded generator (byte-identical CSVs across runs)
  made CI meaningful and bugs reproducible.
- **Graceful degradation in the dashboard.** Guarding every import and data load
  means the app shows a helpful message ("run `make data`") instead of a stack
  trace when a piece is missing.

## What didn't (or was harder than expected)

- _Placeholder — fill in from your build experience, e.g.:_ deciding whether
  `late_settlement` should be checked before or after amount errors (a row can be
  both late *and* off-amount — which label wins?).
- _Placeholder:_ duplicate handling — should the original leg count as `matched`
  and only the extra as `duplicate`, or should both be flagged? (Chose the
  former; documented the choice.)
- _Placeholder:_ dependency wheels on a very new Python (3.14) required pinning
  and verifying availability rather than taking latest.

## Key trade-offs

### Tolerance vs. false-positive rate
The amount tolerance (default **0.01**) is the central knob. Too tight and
ordinary rounding / FX noise floods the breaks queue with false positives that
erode trust; too loose and real leakage (small partial captures, fee creep)
slips through as `matched`. I defaulted tight (0.01) because the synthetic data
is clean, but exposed it as config (`RECON_AMOUNT_TOLERANCE`) precisely because
the right value is dataset-dependent and should be tuned against a labeled
sample, watching precision/recall move.

### SLA threshold vs. alert fatigue
`late_settlement` fires past a 48h SLA. Set it too aggressively and every
weekend batch looks like an incident. Made it config (`RECON_SLA_HOURS`) rather
than a constant.

### Layered/first-fail classification vs. multi-label
A single settlement can violate more than one invariant (late *and* off-amount).
I chose a **first-fail priority order** so every row gets exactly one label —
simpler to action and to score against ground truth — at the cost of hiding
secondary problems. A multi-label mode would be more complete but harder to
triage.

### DuckDB SQL model *and* a Python reconciler
Maintaining the logic twice is duplicated effort, but the SQL model doubles as a
cross-check on the Python implementation and as a realistic "in a warehouse"
reference. Worth it for a portfolio piece; I'd pick one for production.

## What I'd do next

- **LLM-assisted triage.** Use the `ANTHROPIC_API_KEY` hook to draft a plain-
  English root-cause note per break ("fee 4.1% vs. contracted 2.9% + $0.30").
- **Alerting.** Wire the `SLACK_WEBHOOK_URL` sink so a run over threshold pings a
  channel with the top breaks by value-at-risk.
- **Fuzzy / multi-key matching.** Fall back to amount+date proximity when a
  reference is malformed, to recover rows that currently land in `missing_in_*`.
- **Currency awareness.** Handle multi-currency reconciliation with FX rates
  rather than treating amounts as directly comparable across currencies.
- **Trend + drift monitoring.** Track KPIs over time and alert when break_rate or
  value_at_risk drifts, not just on a single run.
- **Real-data adapters.** Import mappers for common PSP export formats (Stripe,
  Adyen, PayPal) so the same engine runs on real settlement files.

---

*Reconcile-Ops is a personal portfolio project. The data is synthetic by design
— see the README's honesty note.*
