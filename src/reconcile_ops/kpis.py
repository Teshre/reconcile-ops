"""KPI computation for a reconciliation run.

:func:`compute_kpis` turns a :class:`~reconcile_ops.reconciler.ReconResult` (plus
the raw input frames and, optionally, ground truth) into a flat dict of the
operational metrics a payments/finance team tracks:

    * match_rate               fraction of settlements that reconcile cleanly
    * break_rate               1 - match_rate
    * settlement_success_rate  share of PSP settlements with status "settled"
    * time_to_settle_p50       median hours from ledger create -> settle
    * time_to_settle_p95       95th-percentile hours to settle
    * cost_per_txn             average PSP fee per settlement
    * value_at_risk            sum of |amount| across all break rows
    * breaks_by_type           per-taxonomy break counts

When ``ground_truth_df`` is supplied it additionally scores the matcher:

    * precision / recall / f1  (macro + per-label), treating every non-"matched"
      label as a positive "break" class, keyed on settlement_id / order_id.

The output is JSON-serialisable (plain floats / ints / strings / dicts) so the
CLI can dump it straight to ``kpis.json``.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .break_detector import BREAK_LABELS
from .reconciler import BREAK_TYPE_COL, ReconResult


def _round(x: float, n: int = 4) -> float:
    """Round for stable, readable JSON (guards against NaN)."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return 0.0
    return round(float(x), n)


def _percentile(series: pd.Series, q: float) -> float:
    """Percentile of a numeric series, NaN-safe. Empty -> 0.0."""
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return 0.0
    return float(clean.quantile(q))


# ---------------------------------------------------------------------------
# Operational KPIs
# ---------------------------------------------------------------------------

def _operational_kpis(
    result: ReconResult, psp_df: pd.DataFrame, ledger_df: pd.DataFrame
) -> dict:
    matched = result.matched_df
    breaks = result.breaks_df

    n_matched = len(matched)
    n_breaks = len(breaks)
    n_decided = n_matched + n_breaks

    # match_rate / break_rate are over all reconciliation *decisions* (every
    # settlement and every ledger orphan lands in exactly one bucket).
    match_rate = (n_matched / n_decided) if n_decided else 0.0
    break_rate = (n_breaks / n_decided) if n_decided else 0.0

    # settlement_success_rate: share of PSP settlements marked "settled".
    if "status" in psp_df.columns and len(psp_df):
        settled_mask = psp_df["status"].astype(str).str.lower().eq("settled")
        settlement_success_rate = float(settled_mask.mean())
    else:
        settlement_success_rate = 0.0

    # time_to_settle: use the settle_hours computed on clean matched rows (both
    # sides present and reconciled) so the timing distribution isn't skewed by
    # amount/duplicate breaks. Fall back to any break rows that carry it.
    settle_hours = pd.Series(dtype="float64")
    if "settle_hours" in matched.columns and len(matched):
        settle_hours = pd.to_numeric(matched["settle_hours"], errors="coerce")
    p50 = _percentile(settle_hours, 0.50)
    p95 = _percentile(settle_hours, 0.95)

    # cost_per_txn: average fee across all PSP settlements.
    if "fee" in psp_df.columns and len(psp_df):
        cost_per_txn = float(pd.to_numeric(psp_df["fee"], errors="coerce").mean())
    else:
        cost_per_txn = 0.0

    # value_at_risk: total absolute money tied up in breaks.
    value_at_risk = 0.0
    if n_breaks and "amount_at_risk" in breaks.columns:
        value_at_risk = float(
            pd.to_numeric(breaks["amount_at_risk"], errors="coerce").abs().sum()
        )

    return {
        "n_psp": int(len(psp_df)),
        "n_ledger": int(len(ledger_df)),
        "n_matched": int(n_matched),
        "n_breaks": int(n_breaks),
        "match_rate": _round(match_rate),
        "break_rate": _round(break_rate),
        "settlement_success_rate": _round(settlement_success_rate),
        "time_to_settle_p50": _round(p50, 2),
        "time_to_settle_p95": _round(p95, 2),
        "cost_per_txn": _round(cost_per_txn, 4),
        "value_at_risk": _round(value_at_risk, 2),
        "breaks_by_type": result.breaks_by_type(),
    }


# ---------------------------------------------------------------------------
# Ground-truth scoring (precision / recall)
# ---------------------------------------------------------------------------

def _predicted_labels(result: ReconResult) -> pd.DataFrame:
    """Flatten a ReconResult into a (key, predicted_label) frame.

    The join key mirrors ground_truth: settlement_id when present, else the
    order_id (missing_in_psp rows have no settlement_id). Matched rows are
    labelled "matched"; break rows carry their break_type.
    """
    records: list[dict] = []

    for _, row in result.matched_df.iterrows():
        records.append(
            {
                "settlement_id": row.get("settlement_id"),
                "order_id": row.get("order_id"),
                "predicted": "matched",
            }
        )
    for _, row in result.breaks_df.iterrows():
        records.append(
            {
                "settlement_id": row.get("settlement_id"),
                "order_id": row.get("order_id"),
                "predicted": row.get(BREAK_TYPE_COL),
            }
        )
    return pd.DataFrame.from_records(
        records, columns=["settlement_id", "order_id", "predicted"]
    )


def _gt_key(row: pd.Series) -> str:
    """Join key for a ground-truth / prediction row.

    Prefer settlement_id; fall back to order_id (for missing_in_psp, where no
    settlement exists). Blank/NaN values normalise to empty string.
    """
    sid = row.get("settlement_id")
    if sid is not None and str(sid).strip() and str(sid) != "nan":
        return f"S::{str(sid).strip()}"
    oid = row.get("order_id")
    if oid is not None and str(oid).strip() and str(oid) != "nan":
        return f"O::{str(oid).strip()}"
    return ""


def _scoring_kpis(result: ReconResult, ground_truth_df: pd.DataFrame) -> dict:
    """Precision/recall/F1 of predictions vs ground truth.

    "Positive" = any break (non-"matched"). We report:
      * binary break-detection precision/recall/f1 (was a break correctly flagged
        as *some* break, regardless of subtype),
      * exact-label accuracy (right break *type*),
      * per-label precision/recall,
      * macro-averaged precision/recall/f1 over the break labels.
    """
    pred = _predicted_labels(result)
    gt = ground_truth_df.copy()

    pred["key"] = pred.apply(_gt_key, axis=1)
    gt["key"] = gt.apply(_gt_key, axis=1)

    pred = pred[pred["key"] != ""]
    gt = gt[gt["key"] != ""]

    merged = gt.merge(
        pred[["key", "predicted"]], on="key", how="outer", indicator=True
    )
    # Rows only in ground truth were never predicted -> treat as "matched"
    # (i.e. the matcher silently reconciled them / never surfaced a break).
    merged["label"] = merged["label"].fillna("matched")
    merged["predicted"] = merged["predicted"].fillna("matched")

    y_true = merged["label"].astype(str)
    y_pred = merged["predicted"].astype(str)

    total = len(merged)

    # -- binary break detection -------------------------------------------
    true_is_break = y_true.isin(BREAK_LABELS)
    pred_is_break = y_pred.isin(BREAK_LABELS)
    tp = int((true_is_break & pred_is_break).sum())
    fp = int((~true_is_break & pred_is_break).sum())
    fn = int((true_is_break & ~pred_is_break).sum())
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    # -- exact label accuracy ---------------------------------------------
    exact_accuracy = float((y_true == y_pred).mean()) if total else 0.0

    # -- per-label precision / recall (over the break taxonomy) -----------
    per_label: dict[str, dict[str, float]] = {}
    prec_list: list[float] = []
    rec_list: list[float] = []
    f1_list: list[float] = []
    for label in sorted(BREAK_LABELS):
        l_tp = int(((y_true == label) & (y_pred == label)).sum())
        l_fp = int(((y_true != label) & (y_pred == label)).sum())
        l_fn = int(((y_true == label) & (y_pred != label)).sum())
        support = int((y_true == label).sum())
        l_prec = l_tp / (l_tp + l_fp) if (l_tp + l_fp) else 0.0
        l_rec = l_tp / (l_tp + l_fn) if (l_tp + l_fn) else 0.0
        l_f1 = (
            2 * l_prec * l_rec / (l_prec + l_rec) if (l_prec + l_rec) else 0.0
        )
        per_label[label] = {
            "precision": _round(l_prec),
            "recall": _round(l_rec),
            "f1": _round(l_f1),
            "support": support,
        }
        if support:  # only average over labels actually present in ground truth
            prec_list.append(l_prec)
            rec_list.append(l_rec)
            f1_list.append(l_f1)

    macro = {
        "precision": _round(sum(prec_list) / len(prec_list) if prec_list else 0.0),
        "recall": _round(sum(rec_list) / len(rec_list) if rec_list else 0.0),
        "f1": _round(sum(f1_list) / len(f1_list) if f1_list else 0.0),
    }

    return {
        "scored_rows": int(total),
        "break_detection": {
            "precision": _round(precision),
            "recall": _round(recall),
            "f1": _round(f1),
            "tp": tp,
            "fp": fp,
            "fn": fn,
        },
        "exact_label_accuracy": _round(exact_accuracy),
        "macro": macro,
        "per_label": per_label,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_kpis(
    recon_result: ReconResult,
    psp_df: pd.DataFrame,
    ledger_df: pd.DataFrame,
    ground_truth_df: Optional[pd.DataFrame] = None,
) -> dict:
    """Compute the full KPI dict for a reconciliation run.

    Parameters
    ----------
    recon_result:
        The :class:`ReconResult` from :meth:`Reconciler.reconcile`.
    psp_df, ledger_df:
        The raw input frames (used for population-level rates: settlement
        success, cost per txn, counts).
    ground_truth_df:
        Optional labels frame (``order_id, settlement_id, label``). When present
        a ``scoring`` block with precision/recall is added.

    Returns
    -------
    dict
        Flat, JSON-serialisable KPI dictionary. Always contains the operational
        metrics; contains a ``scoring`` sub-dict only when ground truth is given.
    """
    kpis = _operational_kpis(recon_result, psp_df, ledger_df)
    if ground_truth_df is not None and len(ground_truth_df):
        kpis["scoring"] = _scoring_kpis(recon_result, ground_truth_df)
    return kpis
