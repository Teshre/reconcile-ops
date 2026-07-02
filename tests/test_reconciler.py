"""Tests for the Reconciler engine.

Two layers of coverage:

  * hand-built micro-fixtures that isolate each set-level break type
    (missing_in_ledger, missing_in_psp, duplicate) plus the pairwise ones, and
    check the partition is complete and non-overlapping;
  * an end-to-end run over the generator's real CSVs, asserting the recovered
    break distribution and (via ground truth) that the matcher is near-perfect.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from reconcile_ops.break_detector import BreakType
from reconcile_ops.config import ReconConfig
from reconcile_ops.reconciler import BREAK_TYPE_COL, Reconciler

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------

def psp_frame(rows: list[dict]) -> pd.DataFrame:
    cols = [
        "settlement_id", "reference", "gross_amount", "fee", "net_amount",
        "currency", "settled_at", "status",
    ]
    return pd.DataFrame(rows, columns=cols)


def ledger_frame(rows: list[dict]) -> pd.DataFrame:
    cols = [
        "order_id", "payment_ref", "expected_amount", "currency",
        "created_at", "status",
    ]
    return pd.DataFrame(rows, columns=cols)


def psp_row(sid, ref, gross, fee=None, net=None, settled="2025-01-01T12:00:00"):
    if fee is None:
        fee = round(gross * 0.029 + 0.30, 2)
    if net is None:
        net = round(gross - fee, 2)
    return {
        "settlement_id": sid, "reference": ref, "gross_amount": gross,
        "fee": fee, "net_amount": net, "currency": "USD",
        "settled_at": settled, "status": "settled",
    }


def ledger_row(oid, ref, expected, created="2025-01-01T00:00:00"):
    return {
        "order_id": oid, "payment_ref": ref, "expected_amount": expected,
        "currency": "USD", "created_at": created, "status": "captured",
    }


def only_type(breaks_df: pd.DataFrame, break_type: BreakType) -> pd.DataFrame:
    return breaks_df[breaks_df[BREAK_TYPE_COL] == break_type.value]


# ---------------------------------------------------------------------------
# per-break-type micro tests
# ---------------------------------------------------------------------------

def test_single_clean_match():
    psp = psp_frame([psp_row("STL-1", "PAY-1", 100.0)])
    ledger = ledger_frame([ledger_row("ORD-1", "PAY-1", 100.0)])
    result = Reconciler(psp, ledger).reconcile()
    assert result.n_matched == 1
    assert result.n_breaks == 0
    assert result.matched_df.iloc[0]["order_id"] == "ORD-1"
    assert result.matched_df.iloc[0]["settlement_id"] == "STL-1"


def test_amount_mismatch_detected():
    psp = psp_frame([psp_row("STL-1", "PAY-1", 200.0)])
    ledger = ledger_frame([ledger_row("ORD-1", "PAY-1", 100.0)])
    result = Reconciler(psp, ledger).reconcile()
    assert result.n_matched == 0
    assert only_type(result.breaks_df, BreakType.AMOUNT_MISMATCH).shape[0] == 1


def test_fee_mismatch_detected():
    psp = psp_frame([psp_row("STL-1", "PAY-1", 100.0, fee=20.0, net=80.0)])
    ledger = ledger_frame([ledger_row("ORD-1", "PAY-1", 100.0)])
    result = Reconciler(psp, ledger).reconcile()
    assert only_type(result.breaks_df, BreakType.FEE_MISMATCH).shape[0] == 1


def test_late_settlement_detected():
    psp = psp_frame([psp_row("STL-1", "PAY-1", 100.0, settled="2025-01-10T00:00:00")])
    ledger = ledger_frame([ledger_row("ORD-1", "PAY-1", 100.0, created="2025-01-01T00:00:00")])
    result = Reconciler(psp, ledger).reconcile()
    assert only_type(result.breaks_df, BreakType.LATE_SETTLEMENT).shape[0] == 1


def test_missing_in_ledger_detected():
    # PSP settlement with a reference the ledger never mentions.
    psp = psp_frame([psp_row("STL-9", "PAY-ORPHAN", 100.0)])
    ledger = ledger_frame([ledger_row("ORD-1", "PAY-1", 100.0)])
    result = Reconciler(psp, ledger).reconcile()
    rows = only_type(result.breaks_df, BreakType.MISSING_IN_LEDGER)
    assert rows.shape[0] == 1
    assert rows.iloc[0]["settlement_id"] == "STL-9"
    assert pd.isna(rows.iloc[0]["order_id"]) or rows.iloc[0]["order_id"] is None
    # also mirrored in the raw unmatched_psp frame
    assert set(result.unmatched_psp_df["reference"]) == {"PAY-ORPHAN"}


def test_missing_in_psp_detected():
    # Ledger order the PSP never settled.
    psp = psp_frame([psp_row("STL-1", "PAY-1", 100.0)])
    ledger = ledger_frame([
        ledger_row("ORD-1", "PAY-1", 100.0),
        ledger_row("ORD-2", "PAY-NOPSP", 50.0),
    ])
    result = Reconciler(psp, ledger).reconcile()
    rows = only_type(result.breaks_df, BreakType.MISSING_IN_PSP)
    assert rows.shape[0] == 1
    assert rows.iloc[0]["order_id"] == "ORD-2"
    assert set(result.unmatched_ledger_df["payment_ref"]) == {"PAY-NOPSP"}


def test_duplicate_detected_keeps_one_match():
    # Same reference on two settlements: original is matched, extra is duplicate.
    psp = psp_frame([
        psp_row("STL-1", "PAY-1", 100.0),
        psp_row("STL-1-DUP", "PAY-1", 100.0, settled="2025-01-01T18:00:00"),
    ])
    ledger = ledger_frame([ledger_row("ORD-1", "PAY-1", 100.0)])
    result = Reconciler(psp, ledger).reconcile()
    assert result.n_matched == 1
    assert result.matched_df.iloc[0]["settlement_id"] == "STL-1"
    dups = only_type(result.breaks_df, BreakType.DUPLICATE)
    assert dups.shape[0] == 1
    assert dups.iloc[0]["settlement_id"] == "STL-1-DUP"


def test_result_is_complete_partition():
    # Every settlement lands in exactly one of matched | breaks.
    psp = psp_frame([
        psp_row("STL-1", "PAY-1", 100.0),                 # matched
        psp_row("STL-2", "PAY-2", 200.0),                 # amount_mismatch
        psp_row("STL-3", "PAY-ORPHAN", 30.0),             # missing_in_ledger
        psp_row("STL-1B", "PAY-1", 100.0, settled="2025-01-01T20:00:00"),  # duplicate
    ])
    ledger = ledger_frame([
        ledger_row("ORD-1", "PAY-1", 100.0),
        ledger_row("ORD-2", "PAY-2", 100.0),
        ledger_row("ORD-3", "PAY-NOPSP", 75.0),           # missing_in_psp
    ])
    result = Reconciler(psp, ledger).reconcile()
    # 4 PSP settlements: 1 matched + 3 break rows; plus 1 ledger orphan.
    n_psp_accounted = result.n_matched + only_type(
        result.breaks_df, BreakType.MISSING_IN_PSP
    ).shape[0]
    # matched(1) + amount(1) + missing_in_ledger(1) + duplicate(1) = 4 PSP rows
    psp_break_rows = result.breaks_df[
        result.breaks_df[BREAK_TYPE_COL] != BreakType.MISSING_IN_PSP.value
    ]
    assert result.n_matched + len(psp_break_rows) == len(psp)
    assert only_type(result.breaks_df, BreakType.MISSING_IN_PSP).shape[0] == 1


def test_config_override_flows_through():
    # 24h settle: late under a 12h SLA, clean under 48h.
    psp = psp_frame([psp_row("STL-1", "PAY-1", 100.0, settled="2025-01-02T00:00:00")])
    ledger = ledger_frame([ledger_row("ORD-1", "PAY-1", 100.0, created="2025-01-01T00:00:00")])
    tight = Reconciler(psp, ledger, ReconConfig(sla_hours=12.0)).reconcile()
    loose = Reconciler(psp, ledger, ReconConfig(sla_hours=48.0)).reconcile()
    assert only_type(tight.breaks_df, BreakType.LATE_SETTLEMENT).shape[0] == 1
    assert loose.n_matched == 1


def test_inputs_not_mutated():
    psp = psp_frame([psp_row("STL-1", "PAY-1", 100.0)])
    ledger = ledger_frame([ledger_row("ORD-1", "PAY-1", 100.0)])
    psp_before = psp.copy(deep=True)
    ledger_before = ledger.copy(deep=True)
    Reconciler(psp, ledger).reconcile()
    pd.testing.assert_frame_equal(psp, psp_before)
    pd.testing.assert_frame_equal(ledger, ledger_before)


def test_empty_inputs():
    result = Reconciler(psp_frame([]), ledger_frame([])).reconcile()
    assert result.n_matched == 0
    assert result.n_breaks == 0


# ---------------------------------------------------------------------------
# end-to-end against the generator's real data + ground truth
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_frames():
    psp = pd.read_csv(DATA_DIR / "psp.csv")
    ledger = pd.read_csv(DATA_DIR / "ledger.csv")
    gt = pd.read_csv(DATA_DIR / "ground_truth.csv")
    return psp, ledger, gt


def test_end_to_end_break_distribution(real_frames):
    psp, ledger, gt = real_frames
    result = Reconciler(psp, ledger).reconcile()

    # Ground-truth break counts (excludes "matched").
    gt_counts = gt[gt["label"] != "matched"]["label"].value_counts().to_dict()
    got = result.breaks_by_type()

    # The recovered per-type counts should match ground truth exactly on this
    # cleanly-separable dataset.
    for label, expected_count in gt_counts.items():
        assert got.get(label, 0) == expected_count, (
            f"{label}: got {got.get(label, 0)}, expected {expected_count}"
        )

    # matched count should equal the ground-truth matched count.
    gt_matched = int((gt["label"] == "matched").sum())
    assert result.n_matched == gt_matched


def test_end_to_end_every_settlement_classified(real_frames):
    psp, ledger, _ = real_frames
    result = Reconciler(psp, ledger).reconcile()
    # Each PSP settlement is either matched or in a PSP-side break row.
    psp_side_breaks = result.breaks_df[
        result.breaks_df[BREAK_TYPE_COL] != BreakType.MISSING_IN_PSP.value
    ]
    assert result.n_matched + len(psp_side_breaks) == len(psp)
