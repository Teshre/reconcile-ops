"""Reconcile-Ops: payments reconciliation + KPI analytics pipeline.

Shared data contract (exact column names used across every component):

  psp.csv          : settlement_id, reference, gross_amount, fee, net_amount,
                     currency, settled_at, status
  ledger.csv       : order_id, payment_ref, expected_amount, currency,
                     created_at, status
  ground_truth.csv : order_id, settlement_id, label

Matching key: psp.reference == ledger.payment_ref.

Break taxonomy (exact enum strings):
  matched, fee_mismatch, amount_mismatch, late_settlement,
  missing_in_ledger, missing_in_psp, duplicate
"""

__version__ = "0.1.0"

from .break_detector import BreakType, classify
from .config import DEFAULT_CONFIG, ReconConfig
from .kpis import compute_kpis
from .reconciler import ReconResult, Reconciler

__all__ = [
    "__version__",
    "BreakType",
    "classify",
    "ReconConfig",
    "DEFAULT_CONFIG",
    "Reconciler",
    "ReconResult",
    "compute_kpis",
]
