"""Tests for the KPI layer.

Covers the arithmetic on tiny hand-built runs (so the expected numbers can be
worked out by hand) and the precision/recall scoring against the generator's
ground truth on the full dataset.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from reconcile_ops.kpis import compute_kpis
from reconcile_ops.reconciler import Reconciler

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def psp_frame(rows):
    cols = [
        "settlement_id", "reference", "gross_amount", "fee", "net_amount",
        "currency", "settled_at", "status",
    ]
    return pd.DataFrame(rows, columns=cols)


def ledger_frame(rows):
    cols = [
        "order_id", "payment_ref", "expected_amount", "currency",
        "created_at", "status",
    ]
    return pd.DataFrame(rows, columns=cols)


def psp_row(sid, ref, gross, fee=None, net=None, settled="2025-01-01T12:00:00",
            status="settled"):
    if fee is None:
        fee = round(gross * 0.029 + 0.30, 2)
    if net is None:
        net = round(gross - fee, 2)
    return {
        "settlement_id": sid, "reference": ref, "gross_amount": gross,
        "fee": fee, "net_amount": net, "currency": "USD",
        "settled_at": settled, "status": status,
    }


def ledger_row(oid, ref, expected, created="2025-01-01T00:00:00"):
    return {
        "order_id": oid, "payment_ref": ref, "expected_amount": expected,
        "currency": "USD", "created_at": created, "status": "captured",
    }


# ---------------------------------------------------------------------------
# operational KPI math on a hand-built run
# ---------------------------------------------------------------------------

@pytest.fixture
def small_run():
    """3 matched + 1 amount_mismatch + 1 missing_in_psp.

    Settlements settle 12h, 6h, 24h after create (all within SLA), fees known.
    """
    psp = psp_frame([
        psp_row("STL-1", "PAY-1", 100.0, fee=3.30, net=96.70, settled="2025-01-01T12:00:00"),
        psp_row("STL-2", "PAY-2", 200.0, fee=6.10, net=193.90, settled="2025-01-01T06:00:00"),
        psp_row("STL-3", "PAY-3", 300.0, fee=9.00, net=291.00, settled="2025-01-02T00:00:00"),
        psp_row("STL-4", "PAY-4", 999.0, fee=29.27, net=969.73),  # amount_mismatch
    ])
    ledger = ledger_frame([
        ledger_row("ORD-1", "PAY-1", 100.0),
        ledger_row("ORD-2", "PAY-2", 200.0),
        ledger_row("ORD-3", "PAY-3", 300.0),
        ledger_row("ORD-4", "PAY-4", 500.0),   # expected 500 vs gross 999 -> mismatch
        ledger_row("ORD-5", "PAY-5", 42.0),     # missing_in_psp
    ])
    result = Reconciler(psp, ledger).reconcile()
    kpis = compute_kpis(result, psp, ledger)
    return result, kpis


def test_counts(small_run):
    _, kpis = small_run
    assert kpis["n_psp"] == 4
    assert kpis["n_ledger"] == 5
    assert kpis["n_matched"] == 3
    # breaks: 1 amount_mismatch + 1 missing_in_psp
    assert kpis["n_breaks"] == 2


def test_match_and_break_rate(small_run):
    _, kpis = small_run
    # decisions = 3 matched + 2 breaks = 5
    assert kpis["match_rate"] == pytest.approx(3 / 5)
    assert kpis["break_rate"] == pytest.approx(2 / 5)
    assert kpis["match_rate"] + kpis["break_rate"] == pytest.approx(1.0)


def test_settlement_success_rate(small_run):
    _, kpis = small_run
    # all 4 PSP rows are "settled"
    assert kpis["settlement_success_rate"] == pytest.approx(1.0)


def test_settlement_success_rate_with_failure():
    psp = psp_frame([
        psp_row("STL-1", "PAY-1", 100.0, status="settled"),
        psp_row("STL-2", "PAY-2", 100.0, status="failed"),
    ])
    ledger = ledger_frame([
        ledger_row("ORD-1", "PAY-1", 100.0),
        ledger_row("ORD-2", "PAY-2", 100.0),
    ])
    result = Reconciler(psp, ledger).reconcile()
    kpis = compute_kpis(result, psp, ledger)
    assert kpis["settlement_success_rate"] == pytest.approx(0.5)


def test_cost_per_txn(small_run):
    _, kpis = small_run
    # mean of fees 3.30, 6.10, 9.00, 29.27
    expected = (3.30 + 6.10 + 9.00 + 29.27) / 4
    assert kpis["cost_per_txn"] == pytest.approx(expected, abs=1e-4)


def test_time_to_settle_percentiles(small_run):
    _, kpis = small_run
    # matched settle_hours: PAY-1=12, PAY-2=6, PAY-3=24
    # p50 (median of 6,12,24) = 12
    assert kpis["time_to_settle_p50"] == pytest.approx(12.0, abs=1e-6)
    # p95 interpolates near the top -> between 12 and 24, close to 24
    assert 12.0 < kpis["time_to_settle_p95"] <= 24.0


def test_value_at_risk(small_run):
    result, kpis = small_run
    # breaks: amount_mismatch amount_at_risk = gross 999.0; missing_in_psp = 42.0
    assert kpis["value_at_risk"] == pytest.approx(999.0 + 42.0, abs=1e-6)


def test_breaks_by_type(small_run):
    _, kpis = small_run
    assert kpis["breaks_by_type"] == {"amount_mismatch": 1, "missing_in_psp": 1}


def test_kpis_json_serialisable(small_run):
    import json
    _, kpis = small_run
    # should not raise
    json.dumps(kpis, default=str)


# ---------------------------------------------------------------------------
# ground-truth scoring on a hand-built run
# ---------------------------------------------------------------------------

def test_scoring_perfect_on_clean_fixture():
    psp = psp_frame([
        psp_row("STL-1", "PAY-1", 100.0),                       # matched
        psp_row("STL-2", "PAY-2", 500.0),                       # amount_mismatch
        psp_row("STL-3", "PAY-ORPHAN", 30.0),                   # missing_in_ledger
    ])
    ledger = ledger_frame([
        ledger_row("ORD-1", "PAY-1", 100.0),
        ledger_row("ORD-2", "PAY-2", 100.0),
        ledger_row("ORD-3", "PAY-NOPSP", 75.0),                 # missing_in_psp
    ])
    gt = pd.DataFrame(
        [
            {"order_id": "ORD-1", "settlement_id": "STL-1", "label": "matched"},
            {"order_id": "ORD-2", "settlement_id": "STL-2", "label": "amount_mismatch"},
            {"order_id": "", "settlement_id": "STL-3", "label": "missing_in_ledger"},
            {"order_id": "ORD-3", "settlement_id": "", "label": "missing_in_psp"},
        ]
    )
    result = Reconciler(psp, ledger).reconcile()
    kpis = compute_kpis(result, psp, ledger, gt)
    scoring = kpis["scoring"]
    assert scoring["break_detection"]["precision"] == pytest.approx(1.0)
    assert scoring["break_detection"]["recall"] == pytest.approx(1.0)
    assert scoring["exact_label_accuracy"] == pytest.approx(1.0)


def test_scoring_penalises_wrong_label():
    # PSP gross matches expected but fee is inflated: reconciler says fee_mismatch.
    # If ground truth (wrongly) calls it amount_mismatch, exact accuracy drops
    # but break-detection recall stays 1 (a break WAS flagged).
    psp = psp_frame([psp_row("STL-1", "PAY-1", 100.0, fee=30.0, net=70.0)])
    ledger = ledger_frame([ledger_row("ORD-1", "PAY-1", 100.0)])
    gt = pd.DataFrame(
        [{"order_id": "ORD-1", "settlement_id": "STL-1", "label": "amount_mismatch"}]
    )
    result = Reconciler(psp, ledger).reconcile()
    kpis = compute_kpis(result, psp, ledger, gt)
    scoring = kpis["scoring"]
    assert scoring["break_detection"]["recall"] == pytest.approx(1.0)
    assert scoring["exact_label_accuracy"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# end-to-end scoring on the generator's full dataset
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_frames():
    psp = pd.read_csv(DATA_DIR / "psp.csv")
    ledger = pd.read_csv(DATA_DIR / "ledger.csv")
    gt = pd.read_csv(DATA_DIR / "ground_truth.csv")
    return psp, ledger, gt


def test_end_to_end_precision_recall(real_frames):
    psp, ledger, gt = real_frames
    result = Reconciler(psp, ledger).reconcile()
    kpis = compute_kpis(result, psp, ledger, gt)
    scoring = kpis["scoring"]

    # The dataset is designed to be cleanly separable; require strong scores.
    assert scoring["break_detection"]["precision"] >= 0.98
    assert scoring["break_detection"]["recall"] >= 0.98
    assert scoring["macro"]["precision"] >= 0.95
    assert scoring["macro"]["recall"] >= 0.95
    assert scoring["exact_label_accuracy"] >= 0.98

    # Every break label present in ground truth should be recalled well.
    for label, m in scoring["per_label"].items():
        if m["support"] > 0:
            assert m["recall"] >= 0.9, f"{label} recall too low: {m['recall']}"


def test_end_to_end_operational_kpis_sane(real_frames):
    psp, ledger, gt = real_frames
    result = Reconciler(psp, ledger).reconcile()
    kpis = compute_kpis(result, psp, ledger, gt)

    assert 0.0 <= kpis["match_rate"] <= 1.0
    assert kpis["match_rate"] + kpis["break_rate"] == pytest.approx(1.0, abs=1e-6)
    assert kpis["match_rate"] > 0.7          # ~80% clean by design
    assert kpis["time_to_settle_p50"] > 0
    assert kpis["time_to_settle_p95"] >= kpis["time_to_settle_p50"]
    assert kpis["cost_per_txn"] > 0
    assert kpis["value_at_risk"] > 0
