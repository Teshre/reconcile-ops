# Reconcile-Ops

Every payment a business takes flows through two systems that are supposed to agree but routinely don't: the payment service provider (PSP / bank) that actually moves the money, and the internal ledger that says what *should* have happened. **Reconcile-Ops** matches those two sources settlement-by-settlement, classifies every discrepancy into an actionable exception type, and quantifies the money at risk — turning a manual month-end spreadsheet chore into a one-command, auditable pipeline.

> Personal portfolio project by **Eduardo Perry Rangel** ([github.com/Teshre](https://github.com/Teshre)). Independent, open source, and built to be read.

---

## Why this matters

Unreconciled payments are where money quietly leaks: a PSP fee that's higher than contracted, a partial capture that never got flagged, a settlement that arrived days late and breached an SLA, a double-settlement that overpaid, an order that was charged but never settled. At scale, "we're off by a few thousand and nobody knows why" is a real, recurring finance-ops problem. Reconcile-Ops makes those breaks **visible, categorized, and priced**.

---

## Architecture

```
        data/psp.csv          data/ledger.csv           data/ground_truth.csv
   (PSP settlement export)   (internal order ledger)    (known labels, synthetic)
              │                      │                             │
              └──────────┬───────────┘                            │
                         ▼                                        │
              ┌─────────────────────┐                            │
              │   Reconciler         │  layered matching on       │
              │  (reconciler.py)     │  reference == payment_ref  │
              └─────────┬───────────┘                            │
                        ▼                                        │
              ┌─────────────────────┐                            │
              │  Break detector      │  classify each row into    │
              │ (break_detector.py)  │  the break taxonomy        │
              └─────────┬───────────┘                            │
                        ▼                                        ▼
              ┌─────────────────────┐              ┌────────────────────────┐
              │  KPI engine          │──────────────│ precision / recall vs  │
              │   (kpis.py)          │              │ ground truth           │
              └─────────┬───────────┘              └────────────────────────┘
                        ▼
        ┌───────────────┴────────────────┐
        ▼                                 ▼
  CLI (cli.py)                    Streamlit dashboard
  out/matched.csv                 (app/streamlit_app.py)
  out/breaks.csv                  KPI cards • filterable breaks
  out/kpis.json                   • settlement trend
```

A parallel **SQL model** (`sql/*.sql`, run via DuckDB directly over the CSVs) implements the same reconciliation logic in set-based SQL — useful as a cross-check and as a "how would this look in a warehouse" reference.

**Repo layout**

```
reconcile-ops/
├── data/generate.py            # seeded synthetic data generator
├── src/reconcile_ops/
│   ├── break_detector.py       # BreakType enum + classify()
│   ├── reconciler.py           # Reconciler -> ReconResult
│   ├── kpis.py                 # compute_kpis()
│   └── cli.py                  # python -m reconcile_ops.cli
├── sql/
│   ├── 01_load.sql             # load CSVs into DuckDB
│   ├── 02_reconcile.sql        # the reconciliation model
│   └── 03_kpis.sql             # KPI rollups
├── tests/                      # pytest: detector, reconciler, KPIs
├── app/streamlit_app.py        # dashboard
└── .github/workflows/ci.yml    # generate data + run tests on push/PR
```

---

## KPIs

All KPIs are computed by `compute_kpis()` and exposed in `out/kpis.json`, the CLI summary, and the dashboard header.

| KPI | What it answers |
|---|---|
| **match_rate** | Share of settlements that reconciled cleanly against the ledger. |
| **break_rate** | Share of settlements flagged as an exception of any type. |
| **settlement_success_rate** | Share of ledger orders that reached a successful settlement. |
| **time_to_settle_p50** (hours) | Median time from ledger creation to PSP settlement. |
| **time_to_settle_p95** (hours) | Tail latency — how slow the slowest settlements are. |
| **cost_per_txn** | Average PSP processing fee per settlement. |
| **value_at_risk** | Total absolute amount tied up in flagged breaks. |
| **precision / recall** | Matcher accuracy vs. ground-truth labels (possible because the sample data is synthetic). |

---

## Quickstart

One command per step, from a clean checkout:

```bash
make setup     # create ./.venv and install duckdb, pandas, streamlit, pytest, faker
make data      # generate the seeded synthetic CSVs into data/
make run       # reconcile -> out/matched.csv, out/breaks.csv, out/kpis.json (+ printed summary)
make app       # launch the Streamlit dashboard
```

Other targets: `make test` (pytest), `make sql` (run the DuckDB model), `make help` (list everything). Requires Python 3.14 (also tested on 3.12 in CI).

---

## How it works

### Matching key

A settlement and a ledger order are the same transaction when **`psp.reference == ledger.payment_ref`**. Everything downstream keys off that join.

### Amount semantics (the crux)

`ledger.expected_amount` is the **gross** order amount the customer was billed. The PSP deducts a fee, so on a clean settlement:

```
psp.gross_amount  ==  ledger.expected_amount        # the order amount agrees
psp.net_amount    ==  psp.gross_amount - psp.fee     # the fee explains the gap
```

Splitting *gross agreement* from *fee/net agreement* is what makes fee problems and amount problems **separable** rather than one blurry "amounts don't match" bucket.

### Layered matching

The reconciler applies checks in order, and the first one that fails determines the break type:

1. **Reference match** — is the reference present on both sides? If only one side has it → `missing_in_ledger` / `missing_in_psp`.
2. **Duplication** — does the same reference / settlement appear more than once? → `duplicate`.
3. **Amount** — does `psp.gross_amount` agree with `ledger.expected_amount` within tolerance (default **0.01**)? If not → `amount_mismatch`. If gross agrees but the fee is implausibly large (net too low) → `fee_mismatch`.
4. **Timeliness** — did the settlement land within the SLA (default **48h** from ledger creation)? If not → `late_settlement`.
5. Otherwise → `matched`.

### Break taxonomy

| Break type | Meaning |
|---|---|
| `matched` | Clean: gross agrees, fee explains net, on time, single occurrence. |
| `fee_mismatch` | Gross agrees but the PSP fee is inflated — net lands too low. |
| `amount_mismatch` | The settled gross itself differs from the expected amount beyond tolerance. |
| `late_settlement` | Amounts reconcile, but settlement breached the SLA window. |
| `missing_in_ledger` | PSP settled money with no matching ledger order (orphan settlement). |
| `missing_in_psp` | Ledger captured an order the PSP never settled. |
| `duplicate` | The same reference settled more than once (double-settlement). |

Tolerance and SLA are configurable (see `.env.example`: `RECON_AMOUNT_TOLERANCE`, `RECON_SLA_HOURS`).

---

## Demo

<!-- DEMO PLACEHOLDER — fill in before sharing -->

- **2-minute walkthrough:** _coming soon_ (link to video)
- **Live dashboard:** _coming soon_ (deployed Streamlit URL)

![Dashboard screenshot placeholder](docs/dashboard.png)

---

## A note on honesty: the data is synthetic

The bundled `data/*.csv` files are **generated**, not real payments data. This is a deliberate design choice, not a shortcut:

- **No privacy or compliance exposure** — there is no real cardholder or merchant data anywhere in this repo.
- **It enables ground truth.** `data/generate.py` injects each break type at a known rate and records the correct label for every row in `ground_truth.csv`. That's what lets the pipeline report honest **precision and recall** for the matcher — something you fundamentally *can't* do on real data where the answer key doesn't exist.
- **It's reproducible.** The generator is fully seeded: same `--seed`, byte-identical CSVs. Anyone can regenerate the exact dataset and verify the numbers.

The reconciliation, KPI, and classification logic is real and would run unchanged against real PSP and ledger exports (upload two CSVs in the dashboard to try). Only the *inputs* are synthetic.

---

## License

Released for portfolio / educational use. See repository for details.
