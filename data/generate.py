#!/usr/bin/env python3
"""Seeded synthetic data generator for Reconcile-Ops.

Produces three CSVs that form the shared data contract for the whole pipeline:

  data/psp.csv          - PSP / bank settlement export
  data/ledger.csv       - internal order ledger
  data/ground_truth.csv - known labels for precision / recall scoring

The generator is fully deterministic: given the same SEED it emits byte-identical
CSVs. It deliberately injects each break type from the taxonomy at a known rate so
the reconciler and KPI layer can be validated against ground truth.

Design notes
------------
* stdlib-only for CSV writing (csv module) plus Faker for realistic-looking refs.
  No pandas dependency at generation time -> fast, few moving parts.
* The matching key is psp.reference == ledger.payment_ref.
* Amount semantics (important — the whole matcher hinges on this):
    ledger.expected_amount is the GROSS order/charge amount (what the customer
    was billed). The PSP deducts a processing fee, so on a clean settlement:
        psp.gross_amount  == ledger.expected_amount          (the order amount)
        psp.net_amount    == psp.gross_amount - psp.fee       (fee explains gap)
  This split is what lets fee_mismatch and amount_mismatch be told apart:
    - fee_mismatch  : gross still matches expected, but the fee (hence net) is off.
    - amount_mismatch: the gross itself differs from expected beyond tolerance.
* "Clean" rows are matched: psp.gross_amount equals ledger.expected_amount within
  tolerance, the fee explains gross-net, and the settlement lands inside the SLA
  window. Every injected break perturbs exactly one of those invariants so the
  label is unambiguous.

Break taxonomy (exact enum strings, shared contract):
  matched, fee_mismatch, amount_mismatch, late_settlement,
  missing_in_ledger, missing_in_psp, duplicate

Run:
    python3 data/generate.py
    python3 data/generate.py --n 5000 --seed 42 --outdir data
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from faker import Faker

# ---------------------------------------------------------------------------
# Configuration / knobs
# ---------------------------------------------------------------------------

DEFAULT_SEED = 42
DEFAULT_N = 5000            # number of ledger orders (base population)
DEFAULT_OUTDIR = "data"

# SLA used by the generator to decide what counts as a "late" settlement.
# The reconciler defaults to the same 48h SLA; keeping them aligned means the
# injected late_settlement rows are exactly the ones the matcher should flag.
SLA_HOURS = 48

CURRENCIES = ["USD", "EUR", "GBP", "MXN"]
# Rough relative weighting so USD dominates but the mix is realistic.
CURRENCY_WEIGHTS = [0.55, 0.25, 0.12, 0.08]

# Target composition of the population. Anything not otherwise injected is a
# clean "matched" row. Rates are approximate (probabilistic draw per order).
BREAK_RATES = {
    "fee_mismatch": 0.04,
    "amount_mismatch": 0.04,
    "late_settlement": 0.05,
    "missing_in_ledger": 0.03,   # settlement exists, no ledger order
    "missing_in_psp": 0.03,      # ledger order exists, no settlement
    "duplicate": 0.02,           # settlement (or ref) appears twice
}
# -> ~79% clean matched rows.

# Break taxonomy as exact strings (kept here so the generator is the single
# source of truth other modules mirror in their BreakType enum).
LABELS = (
    "matched",
    "fee_mismatch",
    "amount_mismatch",
    "late_settlement",
    "missing_in_ledger",
    "missing_in_psp",
    "duplicate",
)

# Column contracts. Other components MUST use these exact headers/order.
PSP_COLUMNS = [
    "settlement_id",
    "reference",
    "gross_amount",
    "fee",
    "net_amount",
    "currency",
    "settled_at",
    "status",
]
LEDGER_COLUMNS = [
    "order_id",
    "payment_ref",
    "expected_amount",
    "currency",
    "created_at",
    "status",
]
GROUND_TRUTH_COLUMNS = [
    "order_id",
    "settlement_id",
    "label",
]


# ---------------------------------------------------------------------------
# Row containers
# ---------------------------------------------------------------------------

@dataclass
class PspRow:
    settlement_id: str
    reference: str
    gross_amount: float
    fee: float
    net_amount: float
    currency: str
    settled_at: str
    status: str


@dataclass
class LedgerRow:
    order_id: str
    payment_ref: str
    expected_amount: float
    currency: str
    created_at: str
    status: str


@dataclass
class GroundTruthRow:
    order_id: str
    settlement_id: str
    label: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _money(x: float) -> float:
    """Round to cents to keep CSVs tidy and comparisons stable."""
    return round(x + 0.0, 2)


def _iso(dt: datetime) -> str:
    """ISO-8601 datetime with seconds, no timezone suffix (naive, UTC-assumed)."""
    return dt.replace(microsecond=0).isoformat()


def _fee_for(gross: float, rng: random.Random) -> float:
    """PSP fee model: ~2.9% + fixed 0.30 (classic card-processing shape)."""
    return _money(gross * 0.029 + 0.30)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate(n: int, seed: int):
    """Build the three record sets. Returns (psp_rows, ledger_rows, gt_rows)."""
    rng = random.Random(seed)
    fake = Faker()
    Faker.seed(seed)

    psp_rows: list[PspRow] = []
    ledger_rows: list[LedgerRow] = []
    gt_rows: list[GroundTruthRow] = []

    # Base "now" is fixed so datetimes are reproducible and independent of when
    # the script is run.
    base_now = datetime(2025, 1, 1, 12, 0, 0)

    def new_reference() -> str:
        # e.g. "PAY-3F2A9C7B" — unique-ish, stable under seed.
        return "PAY-" + fake.hexify(text="^^^^^^^^", upper=True)

    for i in range(n):
        order_id = f"ORD-{i:06d}"
        settlement_id = f"STL-{i:06d}"
        reference = new_reference()
        currency = rng.choices(CURRENCIES, weights=CURRENCY_WEIGHTS, k=1)[0]

        # Order economics: `gross` is the order/charge amount the ledger records
        # as expected_amount. The PSP deducts `fee`; `net` is what actually lands.
        gross = _money(rng.uniform(5.0, 2500.0))
        fee = _fee_for(gross, rng)
        net = _money(gross - fee)

        # created_at spread across ~90 days before base_now.
        created_at = base_now - timedelta(
            days=rng.randint(0, 90),
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
        )
        # Clean settlement lands well within the SLA window.
        settle_delay_h = rng.uniform(1.0, SLA_HOURS - 6.0)
        settled_at = created_at + timedelta(hours=settle_delay_h)

        # Decide the label for this order via a single categorical draw.
        roll = rng.random()
        cumulative = 0.0
        label = "matched"
        for break_label, rate in BREAK_RATES.items():
            cumulative += rate
            if roll < cumulative:
                label = break_label
                break

        # ---- Emit rows per label -------------------------------------------
        if label == "matched":
            # Clean: expected == gross, fee explains gross-net, on time.
            ledger_rows.append(LedgerRow(
                order_id, reference, gross, currency, _iso(created_at), "captured"))
            psp_rows.append(PspRow(
                settlement_id, reference, gross, fee, net, currency,
                _iso(settled_at), "settled"))
            gt_rows.append(GroundTruthRow(order_id, settlement_id, "matched"))

        elif label == "fee_mismatch":
            # Gross still matches the ledger's expected amount, but the PSP fee is
            # inflated -> net lands lower than (gross - plausible_fee). The gross
            # side reconciles; only the fee/net is off. gross-net still equals the
            # (wrong) stated fee, so the discriminator is "fee too large for gross".
            bad_fee = _money(fee + rng.uniform(3.0, 20.0))
            bad_net = _money(gross - bad_fee)
            ledger_rows.append(LedgerRow(
                order_id, reference, gross, currency, _iso(created_at), "captured"))
            psp_rows.append(PspRow(
                settlement_id, reference, gross, bad_fee, bad_net, currency,
                _iso(settled_at), "settled"))
            gt_rows.append(GroundTruthRow(order_id, settlement_id, "fee_mismatch"))

        elif label == "amount_mismatch":
            # The PSP settled a materially different GROSS amount (partial capture,
            # chargeback, FX slip). psp.gross_amount differs from expected beyond
            # tolerance; the fee is a normal fee for that (wrong) gross.
            drift = rng.uniform(10.0, 200.0) * rng.choice([-1, 1])
            bad_gross = _money(max(1.0, gross + drift))
            bad_fee = _fee_for(bad_gross, rng)
            bad_net = _money(bad_gross - bad_fee)
            ledger_rows.append(LedgerRow(
                order_id, reference, gross, currency, _iso(created_at), "captured"))
            psp_rows.append(PspRow(
                settlement_id, reference, bad_gross, bad_fee, bad_net, currency,
                _iso(settled_at), "settled"))
            gt_rows.append(GroundTruthRow(order_id, settlement_id, "amount_mismatch"))

        elif label == "late_settlement":
            # Amounts reconcile perfectly, but settlement lands past the SLA.
            late_delay_h = SLA_HOURS + rng.uniform(6.0, 240.0)
            late_settled = created_at + timedelta(hours=late_delay_h)
            ledger_rows.append(LedgerRow(
                order_id, reference, gross, currency, _iso(created_at), "captured"))
            psp_rows.append(PspRow(
                settlement_id, reference, gross, fee, net, currency,
                _iso(late_settled), "settled"))
            gt_rows.append(GroundTruthRow(order_id, settlement_id, "late_settlement"))

        elif label == "missing_in_ledger":
            # PSP settled money we have no ledger order for (orphan settlement).
            psp_rows.append(PspRow(
                settlement_id, reference, gross, fee, net, currency,
                _iso(settled_at), "settled"))
            # Ground truth keys off the settlement; order_id is blank.
            gt_rows.append(GroundTruthRow("", settlement_id, "missing_in_ledger"))

        elif label == "missing_in_psp":
            # We captured an order but the PSP never settled it.
            ledger_rows.append(LedgerRow(
                order_id, reference, gross, currency, _iso(created_at), "captured"))
            gt_rows.append(GroundTruthRow(order_id, "", "missing_in_psp"))

        elif label == "duplicate":
            # A clean match PLUS a duplicate settlement carrying the same
            # payment reference (double-settlement). Both PSP rows share the ref.
            ledger_rows.append(LedgerRow(
                order_id, reference, gross, currency, _iso(created_at), "captured"))
            psp_rows.append(PspRow(
                settlement_id, reference, gross, fee, net, currency,
                _iso(settled_at), "settled"))
            dup_settlement_id = f"STL-{i:06d}-DUP"
            dup_settled = settled_at + timedelta(hours=rng.uniform(1.0, 12.0))
            psp_rows.append(PspRow(
                dup_settlement_id, reference, gross, fee, net, currency,
                _iso(dup_settled), "settled"))
            # The original leg is a clean match; the extra leg is the duplicate.
            gt_rows.append(GroundTruthRow(order_id, settlement_id, "matched"))
            gt_rows.append(GroundTruthRow(order_id, dup_settlement_id, "duplicate"))

    # Shuffle each side independently so row order carries no signal (real
    # exports are not aligned). Deterministic under the seeded RNG.
    rng.shuffle(psp_rows)
    rng.shuffle(ledger_rows)

    return psp_rows, ledger_rows, gt_rows


# ---------------------------------------------------------------------------
# CSV writing
# ---------------------------------------------------------------------------

def _write_csv(path: str, columns: list[str], rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: getattr(row, c) for c in columns})


def write_all(outdir: str, psp_rows, ledger_rows, gt_rows) -> dict[str, str]:
    os.makedirs(outdir, exist_ok=True)
    paths = {
        "psp": os.path.join(outdir, "psp.csv"),
        "ledger": os.path.join(outdir, "ledger.csv"),
        "ground_truth": os.path.join(outdir, "ground_truth.csv"),
    }
    _write_csv(paths["psp"], PSP_COLUMNS, psp_rows)
    _write_csv(paths["ledger"], LEDGER_COLUMNS, ledger_rows)
    _write_csv(paths["ground_truth"], GROUND_TRUTH_COLUMNS, gt_rows)
    return paths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Generate seeded reconciliation data.")
    p.add_argument("--n", type=int, default=DEFAULT_N,
                   help=f"number of base ledger orders (default {DEFAULT_N})")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help=f"random seed (default {DEFAULT_SEED})")
    p.add_argument("--outdir", default=DEFAULT_OUTDIR,
                   help=f"output directory (default {DEFAULT_OUTDIR})")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    psp_rows, ledger_rows, gt_rows = generate(args.n, args.seed)
    paths = write_all(args.outdir, psp_rows, ledger_rows, gt_rows)

    # Report row counts and the injected label distribution.
    dist: dict[str, int] = {label: 0 for label in LABELS}
    for gt in gt_rows:
        dist[gt.label] = dist.get(gt.label, 0) + 1

    total_labels = sum(dist.values())
    print("Reconcile-Ops data generator")
    print(f"  seed={args.seed}  n={args.n}")
    print("  files written:")
    print(f"    {paths['psp']:<28} rows={len(psp_rows)}")
    print(f"    {paths['ledger']:<28} rows={len(ledger_rows)}")
    print(f"    {paths['ground_truth']:<28} rows={len(gt_rows)}")
    print("  injected label distribution (ground truth):")
    for label in LABELS:
        count = dist.get(label, 0)
        pct = (100.0 * count / total_labels) if total_labels else 0.0
        print(f"    {label:<20} {count:>6}  ({pct:5.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
