"""Unit tests for the row-level break classifier.

These use tiny hand-built dict fixtures so each assertion pins down exactly one
axis of the discriminator (amount, fee, SLA). The set-level outcomes
(missing_*, duplicate) are covered in test_reconciler.py where they belong.
"""

from __future__ import annotations

import pytest

from reconcile_ops.break_detector import BreakType, classify
from reconcile_ops.config import ReconConfig

CONFIG = ReconConfig()  # generator-aligned defaults: SLA=48h, tol=0.01


def make_pair(
    *,
    gross: float = 100.0,
    fee: float = 3.20,
    net: float | None = None,
    expected: float = 100.0,
    created_at: str = "2025-01-01T00:00:00",
    settled_at: str = "2025-01-01T12:00:00",
):
    """Build a (psp_row, ledger_row) dict pair with sensible clean defaults.

    Default fee 3.20 == 100*0.029 + 0.30 exactly (the modelled clean fee).
    """
    if net is None:
        net = round(gross - fee, 2)
    psp_row = {
        "settlement_id": "STL-1",
        "reference": "PAY-1",
        "gross_amount": gross,
        "fee": fee,
        "net_amount": net,
        "currency": "USD",
        "settled_at": settled_at,
        "status": "settled",
    }
    ledger_row = {
        "order_id": "ORD-1",
        "payment_ref": "PAY-1",
        "expected_amount": expected,
        "currency": "USD",
        "created_at": created_at,
        "status": "captured",
    }
    return psp_row, ledger_row


# ---------------------------------------------------------------------------
# BreakType enum contract
# ---------------------------------------------------------------------------

def test_breaktype_string_values_match_contract():
    assert BreakType.MATCHED == "matched"
    assert BreakType.FEE_MISMATCH == "fee_mismatch"
    assert BreakType.AMOUNT_MISMATCH == "amount_mismatch"
    assert BreakType.LATE_SETTLEMENT == "late_settlement"
    assert BreakType.MISSING_IN_LEDGER == "missing_in_ledger"
    assert BreakType.MISSING_IN_PSP == "missing_in_psp"
    assert BreakType.DUPLICATE == "duplicate"


def test_is_break_flag():
    assert BreakType.MATCHED.is_break is False
    for bt in BreakType:
        if bt is not BreakType.MATCHED:
            assert bt.is_break is True


# ---------------------------------------------------------------------------
# matched (clean)
# ---------------------------------------------------------------------------

def test_clean_pair_is_matched():
    psp, ledger = make_pair()
    assert classify(psp, ledger, CONFIG) is BreakType.MATCHED


def test_matched_within_amount_tolerance():
    # gross differs from expected by well under the 0.01 tolerance -> matched.
    # (Exactly 0.01 sits on the IEEE-754 boundary; use a clearly-inside value.)
    psp, ledger = make_pair(gross=100.005, expected=100.00, fee=3.20)
    # keep fee/net consistent for the (slightly-off) gross
    psp["net_amount"] = round(100.005 - 3.20, 2)
    assert classify(psp, ledger, CONFIG) is BreakType.MATCHED


def test_settlement_at_exactly_sla_is_matched():
    # 48h exactly is within SLA (strictly-greater-than is late).
    psp, ledger = make_pair(
        created_at="2025-01-01T00:00:00", settled_at="2025-01-03T00:00:00"
    )
    assert classify(psp, ledger, CONFIG) is BreakType.MATCHED


# ---------------------------------------------------------------------------
# amount_mismatch
# ---------------------------------------------------------------------------

def test_amount_mismatch_gross_too_high():
    psp, ledger = make_pair(gross=150.0, expected=100.0)
    assert classify(psp, ledger, CONFIG) is BreakType.AMOUNT_MISMATCH


def test_amount_mismatch_gross_too_low():
    psp, ledger = make_pair(gross=60.0, expected=100.0)
    assert classify(psp, ledger, CONFIG) is BreakType.AMOUNT_MISMATCH


def test_amount_mismatch_takes_priority_over_late():
    # Both a big amount gap AND a late settlement: amount wins (more fundamental).
    psp, ledger = make_pair(
        gross=200.0,
        expected=100.0,
        created_at="2025-01-01T00:00:00",
        settled_at="2025-01-10T00:00:00",
    )
    assert classify(psp, ledger, CONFIG) is BreakType.AMOUNT_MISMATCH


# ---------------------------------------------------------------------------
# fee_mismatch
# ---------------------------------------------------------------------------

def test_fee_mismatch_inflated_fee():
    # Gross reconciles, but fee is way over the ~2.9%+0.30 model -> net too low.
    psp, ledger = make_pair(gross=100.0, fee=15.0, expected=100.0)
    assert classify(psp, ledger, CONFIG) is BreakType.FEE_MISMATCH


def test_fee_within_tolerance_is_matched():
    # Modelled fee for 100 is 3.20; +0.50 tolerance -> up to 3.70 is fine.
    psp, ledger = make_pair(gross=100.0, fee=3.60, expected=100.0)
    assert classify(psp, ledger, CONFIG) is BreakType.MATCHED


def test_fee_mismatch_takes_priority_over_late():
    psp, ledger = make_pair(
        gross=100.0,
        fee=25.0,
        expected=100.0,
        created_at="2025-01-01T00:00:00",
        settled_at="2025-01-10T00:00:00",
    )
    assert classify(psp, ledger, CONFIG) is BreakType.FEE_MISMATCH


def test_inconsistent_net_flagged_as_fee_mismatch():
    # gross - net != fee (net doesn't reconcile against the stated fee).
    psp, ledger = make_pair(gross=100.0, fee=3.20, net=50.0, expected=100.0)
    assert classify(psp, ledger, CONFIG) is BreakType.FEE_MISMATCH


# ---------------------------------------------------------------------------
# late_settlement
# ---------------------------------------------------------------------------

def test_late_settlement_beyond_sla():
    psp, ledger = make_pair(
        created_at="2025-01-01T00:00:00", settled_at="2025-01-05T00:00:00"
    )
    assert classify(psp, ledger, CONFIG) is BreakType.LATE_SETTLEMENT


def test_late_settlement_just_over_sla():
    # 48h + 1s -> late.
    psp, ledger = make_pair(
        created_at="2025-01-01T00:00:00", settled_at="2025-01-03T00:00:01"
    )
    assert classify(psp, ledger, CONFIG) is BreakType.LATE_SETTLEMENT


# ---------------------------------------------------------------------------
# config sensitivity
# ---------------------------------------------------------------------------

def test_custom_sla_changes_outcome():
    psp, ledger = make_pair(
        created_at="2025-01-01T00:00:00", settled_at="2025-01-02T00:00:00"
    )  # 24h
    tight = ReconConfig(sla_hours=12.0)
    loose = ReconConfig(sla_hours=48.0)
    assert classify(psp, ledger, tight) is BreakType.LATE_SETTLEMENT
    assert classify(psp, ledger, loose) is BreakType.MATCHED


def test_custom_amount_tolerance():
    psp, ledger = make_pair(gross=100.5, expected=100.0, fee=3.20)
    psp["net_amount"] = round(100.5 - 3.20, 2)
    strict = ReconConfig(amount_tolerance=0.01)
    loose = ReconConfig(amount_tolerance=1.0)
    assert classify(psp, ledger, strict) is BreakType.AMOUNT_MISMATCH
    assert classify(psp, ledger, loose) is BreakType.MATCHED


def test_classify_uses_default_config_when_none():
    psp, ledger = make_pair()
    assert classify(psp, ledger) is BreakType.MATCHED


# ---------------------------------------------------------------------------
# parsing robustness
# ---------------------------------------------------------------------------

def test_string_amounts_are_coerced():
    psp, ledger = make_pair()
    psp["gross_amount"] = "100.00"
    psp["fee"] = "3.20"
    psp["net_amount"] = "96.80"
    ledger["expected_amount"] = "100.00"
    assert classify(psp, ledger, CONFIG) is BreakType.MATCHED


def test_bad_amount_raises():
    psp, ledger = make_pair()
    psp["gross_amount"] = "not-a-number"
    with pytest.raises(ValueError):
        classify(psp, ledger, CONFIG)
