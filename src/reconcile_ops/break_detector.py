"""Row-level break classification.

This module owns the break taxonomy (:class:`BreakType`) and the single-pair
decision function :func:`classify`, which decides what kind of break — if any —
a matched (psp_row, ledger_row) pair exhibits.

``classify`` is the *core discriminator* for the four "both sides present"
outcomes:

    matched, fee_mismatch, amount_mismatch, late_settlement

The set-level outcomes — ``missing_in_ledger``, ``missing_in_psp`` and
``duplicate`` — depend on the whole population (a reference present on one side
only, or a reference appearing on multiple settlements) and are therefore
decided by the :class:`~reconcile_ops.reconciler.Reconciler`, not here. They are
still members of :class:`BreakType` so the enum is the one true taxonomy.

Amount semantics (the whole matcher hinges on this):
    ``ledger.expected_amount`` is the GROSS order amount the customer was
    billed. On a clean settlement::

        psp.gross_amount == ledger.expected_amount     (within tolerance)
        psp.gross_amount - psp.net_amount == psp.fee    (fee explains the gap)

    * ``amount_mismatch``: ``psp.gross_amount`` itself diverges from
      ``expected_amount`` beyond tolerance (partial capture, chargeback, FX).
    * ``fee_mismatch``: gross still reconciles, but the stated ``fee`` exceeds a
      plausible fee (~2.9% + 0.30) — net lands too low. Only the fee/net is off.

Evaluation order matters: an amount discrepancy is a more fundamental break than
a fee discrepancy, which is more fundamental than a lateness breach. The first
failing check wins.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional

from .config import DEFAULT_CONFIG, ReconConfig


class BreakType(str, Enum):
    """The reconciliation break taxonomy (exact contract strings).

    Subclassing ``str`` means members compare equal to their raw string value
    (``BreakType.MATCHED == "matched"``) and serialise cleanly to CSV/JSON,
    while still giving us an enum's safety and autocompletion.
    """

    MATCHED = "matched"
    FEE_MISMATCH = "fee_mismatch"
    AMOUNT_MISMATCH = "amount_mismatch"
    LATE_SETTLEMENT = "late_settlement"
    MISSING_IN_LEDGER = "missing_in_ledger"
    MISSING_IN_PSP = "missing_in_psp"
    DUPLICATE = "duplicate"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value

    @property
    def is_break(self) -> bool:
        """True for every outcome except a clean :attr:`MATCHED`."""
        return self is not BreakType.MATCHED


# Convenience: the set of labels that represent an actual break (not matched).
BREAK_LABELS = frozenset(bt.value for bt in BreakType if bt.is_break)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _to_float(value: Any) -> float:
    """Coerce a cell to float, raising a clear error on bad input."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected a numeric amount, got {value!r}") from exc


def _to_datetime(value: Any) -> datetime:
    """Parse a naive ISO-8601 datetime (``YYYY-MM-DDTHH:MM:SS``).

    Accepts already-parsed ``datetime`` objects (e.g. pandas Timestamps) and
    tolerates a trailing ``Z`` / space-separated form for robustness.
    """
    if isinstance(value, datetime):
        return value
    # pandas Timestamp exposes .to_pydatetime()
    to_py = getattr(value, "to_pydatetime", None)
    if callable(to_py):
        return to_py()
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1]
    text = text.replace(" ", "T", 1) if "T" not in text and " " in text else text
    return datetime.fromisoformat(text)


def _hours_between(created_at: Any, settled_at: Any) -> float:
    """Signed hours from ``created_at`` to ``settled_at``."""
    start = _to_datetime(created_at)
    end = _to_datetime(settled_at)
    return (end - start).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# The discriminator
# ---------------------------------------------------------------------------

def classify(
    psp_row: Mapping[str, Any],
    ledger_row: Mapping[str, Any],
    config: Optional[ReconConfig] = None,
) -> BreakType:
    """Classify a single matched (psp_row, ledger_row) pair.

    Both arguments are mappings using the shared column contract — a plain dict
    or a pandas ``Series`` both work.

    Layered checks, first failure wins:

    1. **amount_mismatch** — ``psp.gross_amount`` differs from
       ``ledger.expected_amount`` beyond :attr:`ReconConfig.amount_tolerance`.
    2. **fee_mismatch** — gross reconciles, but the stated ``fee`` exceeds the
       modelled plausible fee (``expected_fee(gross) + fee_tolerance``), i.e.
       net landed too low. The ``gross - net == fee`` identity is respected: we
       look at whether the fee itself is implausibly large, not at rounding.
    3. **late_settlement** — amounts clean, but ``settled_at - created_at``
       exceeds :attr:`ReconConfig.sla_hours`.
    4. **matched** — everything reconciles.

    Returns one of :attr:`BreakType.MATCHED`, :attr:`~BreakType.AMOUNT_MISMATCH`,
    :attr:`~BreakType.FEE_MISMATCH`, :attr:`~BreakType.LATE_SETTLEMENT`. The
    set-level outcomes are never returned here (see module docstring).
    """
    cfg = config or DEFAULT_CONFIG

    gross = _to_float(psp_row["gross_amount"])
    net = _to_float(psp_row["net_amount"])
    fee = _to_float(psp_row["fee"])
    expected = _to_float(ledger_row["expected_amount"])

    # (1) Gross vs expected. This is the most fundamental discrepancy: the PSP
    # settled a different order amount than the ledger booked.
    if not cfg.amounts_equal(gross, expected):
        return BreakType.AMOUNT_MISMATCH

    # (2) Fee plausibility. Gross reconciles; is the fee (hence net) sane?
    # A stated fee materially larger than the modelled fee means net landed too
    # low — the PSP over-charged / under-remitted.
    plausible_fee_ceiling = cfg.expected_fee(gross) + cfg.fee_tolerance
    if fee > plausible_fee_ceiling:
        return BreakType.FEE_MISMATCH

    # Guard the gross/net/fee identity too: if net doesn't equal gross-fee within
    # tolerance the row is internally inconsistent, which we surface as a fee
    # break (the fee does not explain the gap). This rarely fires on generator
    # data but protects against malformed real exports.
    if not cfg.amounts_equal(gross - net, fee):
        return BreakType.FEE_MISMATCH

    # (3) SLA / lateness. Amounts are clean; did it settle in time?
    if _hours_between(ledger_row["created_at"], psp_row["settled_at"]) > cfg.sla_hours:
        return BreakType.LATE_SETTLEMENT

    # (4) Nothing tripped — clean match.
    return BreakType.MATCHED
