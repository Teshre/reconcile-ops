"""The reconciliation engine.

:class:`Reconciler` takes a PSP settlement export and an internal ledger (both as
pandas DataFrames on the shared column contract) and produces a
:class:`ReconResult`: the matched pairs, the classified breaks, and the rows that
could not be paired at all.

Matching strategy (layered, per the contract)::

    1. Exact join on the reference key   psp.reference == ledger.payment_ref
    2. Duplicate detection               a reference on >1 settlement (or >1
                                         ledger order) is a `duplicate`
    3. Amount / fee / SLA classification delegated per-pair to break_detector
    4. One-sided references               reference only in PSP  -> missing_in_ledger
                                          reference only in ledger-> missing_in_psp

Every settlement and every ledger order ends up in exactly one bucket, so the
result is a complete, non-overlapping partition of the input — which is what lets
the KPI layer compute rates and score against ground truth without double
counting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from .break_detector import BreakType, classify
from .config import DEFAULT_CONFIG, ReconConfig

# The column emitted on every breaks row naming its taxonomy label.
BREAK_TYPE_COL = "break_type"


@dataclass
class ReconResult:
    """Outcome of a reconciliation run.

    Attributes
    ----------
    matched_df:
        One row per clean matched pair. Carries the joined PSP + ledger columns
        plus ``settle_hours`` (hours from ``created_at`` to ``settled_at``).
    breaks_df:
        One row per detected break, with a ``break_type`` column drawn from the
        taxonomy. Contains whatever side(s) of the pair exist for that break
        (both sides for amount/fee/late/duplicate; PSP-only for
        ``missing_in_ledger``; ledger-only for ``missing_in_psp``).
    unmatched_psp_df:
        PSP settlements whose reference never appears in the ledger. These are
        also represented in ``breaks_df`` as ``missing_in_ledger``; this frame
        is the raw PSP-side view for convenience.
    unmatched_ledger_df:
        Ledger orders whose reference never appears in the PSP export
        (``missing_in_psp`` mirror).
    config:
        The :class:`ReconConfig` used for the run (echoed for provenance).
    """

    matched_df: pd.DataFrame
    breaks_df: pd.DataFrame
    unmatched_psp_df: pd.DataFrame
    unmatched_ledger_df: pd.DataFrame
    config: ReconConfig = field(default_factory=lambda: DEFAULT_CONFIG)

    # -- convenience views -------------------------------------------------
    @property
    def n_matched(self) -> int:
        return len(self.matched_df)

    @property
    def n_breaks(self) -> int:
        return len(self.breaks_df)

    def breaks_by_type(self) -> dict[str, int]:
        """Count of break rows per taxonomy label (stable key order)."""
        if self.breaks_df.empty:
            return {}
        counts = self.breaks_df[BREAK_TYPE_COL].value_counts()
        return {str(k): int(v) for k, v in counts.items()}


class Reconciler:
    """Reconcile a PSP export against an internal ledger.

    Parameters
    ----------
    psp_df, ledger_df:
        Input frames on the shared column contract. They are defensively copied
        and normalised (types coerced, whitespace stripped from keys) so the
        caller's frames are never mutated.
    config:
        Optional :class:`ReconConfig`; defaults to :data:`DEFAULT_CONFIG`.
    """

    #: Amount columns coerced to numeric on load.
    _PSP_NUMERIC = ("gross_amount", "fee", "net_amount")
    _LEDGER_NUMERIC = ("expected_amount",)
    #: Datetime columns coerced on load.
    _PSP_DATETIME = ("settled_at",)
    _LEDGER_DATETIME = ("created_at",)

    def __init__(
        self,
        psp_df: pd.DataFrame,
        ledger_df: pd.DataFrame,
        config: Optional[ReconConfig] = None,
    ) -> None:
        self.config = config or DEFAULT_CONFIG
        self.psp_df = self._prepare_psp(psp_df)
        self.ledger_df = self._prepare_ledger(ledger_df)

    # ------------------------------------------------------------------ load
    @staticmethod
    def _coerce(df: pd.DataFrame, numeric, datetime_cols) -> pd.DataFrame:
        out = df.copy()
        for col in numeric:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        for col in datetime_cols:
            if col in out.columns:
                out[col] = pd.to_datetime(out[col], errors="coerce")
        return out

    def _prepare_psp(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._coerce(df, self._PSP_NUMERIC, self._PSP_DATETIME)
        if "reference" in out.columns:
            out["reference"] = out["reference"].astype(str).str.strip()
        return out.reset_index(drop=True)

    def _prepare_ledger(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._coerce(df, self._LEDGER_NUMERIC, self._LEDGER_DATETIME)
        if "payment_ref" in out.columns:
            out["payment_ref"] = out["payment_ref"].astype(str).str.strip()
        return out.reset_index(drop=True)

    # -------------------------------------------------------------- reconcile
    def reconcile(self) -> ReconResult:
        """Run the full layered reconciliation and return a :class:`ReconResult`."""
        psp = self.psp_df
        ledger = self.ledger_df

        psp_refs = set(psp["reference"])
        ledger_refs = set(ledger["payment_ref"])

        # (A) Duplicate references. A reference carried by >1 settlement is a
        # double-settlement; a reference on >1 ledger order is a double-booking.
        # Both surface as `duplicate`.
        psp_ref_counts = psp["reference"].value_counts()
        ledger_ref_counts = ledger["payment_ref"].value_counts()
        dup_psp_refs = set(psp_ref_counts[psp_ref_counts > 1].index)
        dup_ledger_refs = set(ledger_ref_counts[ledger_ref_counts > 1].index)
        duplicate_refs = dup_psp_refs | dup_ledger_refs

        matched_rows: list[dict] = []
        break_rows: list[dict] = []

        # (B) Walk every PSP settlement. Its reference determines the bucket.
        for _, psp_row in psp.iterrows():
            ref = psp_row["reference"]

            if ref not in ledger_refs:
                # Reference exists only on the PSP side -> orphan settlement.
                break_rows.append(self._psp_break_row(psp_row, BreakType.MISSING_IN_LEDGER))
                continue

            if ref in duplicate_refs:
                # Part of a duplicate cluster. Keep exactly one leg as the clean
                # match (the lexicographically-first settlement_id, which is the
                # original before the "-DUP" suffix) and flag the rest.
                if self._is_primary_settlement(psp, ref, psp_row):
                    ledger_row = self._first_ledger_for(ledger, ref)
                    self._route_pair(psp_row, ledger_row, matched_rows, break_rows)
                else:
                    break_rows.append(
                        self._pair_break_row(
                            psp_row,
                            self._first_ledger_for(ledger, ref),
                            BreakType.DUPLICATE,
                        )
                    )
                continue

            # Normal 1:1 reference. Classify the pair.
            ledger_row = self._first_ledger_for(ledger, ref)
            self._route_pair(psp_row, ledger_row, matched_rows, break_rows)

        # (C) Ledger orders whose reference never appears in the PSP export.
        for _, ledger_row in ledger.iterrows():
            if ledger_row["payment_ref"] not in psp_refs:
                break_rows.append(
                    self._ledger_break_row(ledger_row, BreakType.MISSING_IN_PSP)
                )

        matched_df = pd.DataFrame(matched_rows)
        breaks_df = pd.DataFrame(break_rows)

        unmatched_psp_df = psp[~psp["reference"].isin(ledger_refs)].reset_index(drop=True)
        unmatched_ledger_df = ledger[
            ~ledger["payment_ref"].isin(psp_refs)
        ].reset_index(drop=True)

        return ReconResult(
            matched_df=matched_df,
            breaks_df=breaks_df,
            unmatched_psp_df=unmatched_psp_df,
            unmatched_ledger_df=unmatched_ledger_df,
            config=self.config,
        )

    # ----------------------------------------------------------- routing
    def _route_pair(self, psp_row, ledger_row, matched_rows, break_rows) -> None:
        """Classify a paired settlement and file it under matched or breaks."""
        outcome = classify(psp_row, ledger_row, self.config)
        if outcome is BreakType.MATCHED:
            matched_rows.append(self._matched_row(psp_row, ledger_row))
        else:
            break_rows.append(self._pair_break_row(psp_row, ledger_row, outcome))

    @staticmethod
    def _is_primary_settlement(psp: pd.DataFrame, ref: str, psp_row) -> bool:
        """Is this the primary (original) leg of a duplicate cluster?

        The generator suffixes extra legs with ``-DUP``; sorting settlement_ids
        ascending puts the un-suffixed original first. We treat the smallest
        settlement_id in the cluster as the keeper.
        """
        cluster = psp[psp["reference"] == ref]
        primary_id = cluster["settlement_id"].min()
        return psp_row["settlement_id"] == primary_id

    @staticmethod
    def _first_ledger_for(ledger: pd.DataFrame, ref: str):
        """Return the first ledger row for a reference (as a Series)."""
        return ledger[ledger["payment_ref"] == ref].iloc[0]

    # -------------------------------------------------------- row builders
    @staticmethod
    def _settle_hours(created_at, settled_at) -> float:
        delta = pd.Timestamp(settled_at) - pd.Timestamp(created_at)
        return delta.total_seconds() / 3600.0

    def _matched_row(self, psp_row, ledger_row) -> dict:
        return {
            "settlement_id": psp_row["settlement_id"],
            "reference": psp_row["reference"],
            "order_id": ledger_row["order_id"],
            "gross_amount": float(psp_row["gross_amount"]),
            "expected_amount": float(ledger_row["expected_amount"]),
            "fee": float(psp_row["fee"]),
            "net_amount": float(psp_row["net_amount"]),
            "currency": psp_row["currency"],
            "created_at": ledger_row["created_at"],
            "settled_at": psp_row["settled_at"],
            "settle_hours": self._settle_hours(
                ledger_row["created_at"], psp_row["settled_at"]
            ),
        }

    def _pair_break_row(self, psp_row, ledger_row, break_type: BreakType) -> dict:
        """Break row that has both a PSP and a ledger side."""
        settle_hours = self._settle_hours(
            ledger_row["created_at"], psp_row["settled_at"]
        )
        return {
            BREAK_TYPE_COL: break_type.value,
            "settlement_id": psp_row["settlement_id"],
            "order_id": ledger_row["order_id"],
            "reference": psp_row["reference"],
            "gross_amount": float(psp_row["gross_amount"]),
            "expected_amount": float(ledger_row["expected_amount"]),
            "fee": float(psp_row["fee"]),
            "net_amount": float(psp_row["net_amount"]),
            "currency": psp_row["currency"],
            "created_at": ledger_row["created_at"],
            "settled_at": psp_row["settled_at"],
            "settle_hours": settle_hours,
            # Amount "at risk" for this break: the gross the PSP moved.
            "amount_at_risk": float(psp_row["gross_amount"]),
        }

    def _psp_break_row(self, psp_row, break_type: BreakType) -> dict:
        """Break row for a PSP-only orphan (missing_in_ledger)."""
        return {
            BREAK_TYPE_COL: break_type.value,
            "settlement_id": psp_row["settlement_id"],
            "order_id": None,
            "reference": psp_row["reference"],
            "gross_amount": float(psp_row["gross_amount"]),
            "expected_amount": None,
            "fee": float(psp_row["fee"]),
            "net_amount": float(psp_row["net_amount"]),
            "currency": psp_row["currency"],
            "created_at": None,
            "settled_at": psp_row["settled_at"],
            "settle_hours": None,
            "amount_at_risk": float(psp_row["gross_amount"]),
        }

    def _ledger_break_row(self, ledger_row, break_type: BreakType) -> dict:
        """Break row for a ledger-only orphan (missing_in_psp)."""
        return {
            BREAK_TYPE_COL: break_type.value,
            "settlement_id": None,
            "order_id": ledger_row["order_id"],
            "reference": ledger_row["payment_ref"],
            "gross_amount": None,
            "expected_amount": float(ledger_row["expected_amount"]),
            "fee": None,
            "net_amount": None,
            "currency": ledger_row["currency"],
            "created_at": ledger_row["created_at"],
            "settled_at": None,
            "settle_hours": None,
            "amount_at_risk": float(ledger_row["expected_amount"]),
        }
