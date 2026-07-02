"""Reconcile-Ops — Streamlit dashboard.

Runs the reconciliation pipeline over the bundled synthetic sample data (or a
pair of user-uploaded CSVs) and surfaces the operational picture a payments /
finance-ops team actually cares about:

    * KPI header cards      — match rate, break rate, settlement success,
                              time-to-settle p95, cost per txn, value-at-risk
    * a filterable breaks   — every exception the matcher flagged, filterable by
      table                   break_type so an analyst can work one queue at a time
    * a settlement trend    — settled value / volume over time

Design goals
------------
* **Degrade gracefully.** The reconcile_ops package, pandas, and the sample data
  may or may not be present in the environment the dashboard is launched in.
  Every dependency and data source is imported/loaded lazily behind a guard so a
  missing piece produces a friendly in-app message instead of a stack trace.
* **Zero contract drift.** The app never hard-codes column names it can avoid;
  it leans on the reconcile_ops package (the single source of truth for the
  schema and the break taxonomy) and only falls back to the documented headers.

Run:
    PYTHONPATH=src streamlit run app/streamlit_app.py
    # or simply:  make app
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Path / import bootstrapping
# ---------------------------------------------------------------------------
# The package lives under src/. When the app is launched via `make app` the
# Makefile exports PYTHONPATH=src, but we also add it here so a bare
# `streamlit run app/streamlit_app.py` from the repo root still works.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

DATA_DIR = REPO_ROOT / "data"
DEFAULT_PSP = DATA_DIR / "psp.csv"
DEFAULT_LEDGER = DATA_DIR / "ledger.csv"
DEFAULT_GROUND_TRUTH = DATA_DIR / "ground_truth.csv"

# Documented column contracts — used only as a fallback for display niceties if
# the package can't be imported. The generator / package remain the source of
# truth; these mirror src/reconcile_ops/__init__.py.
PSP_COLUMNS = [
    "settlement_id", "reference", "gross_amount", "fee", "net_amount",
    "currency", "settled_at", "status",
]
LEDGER_COLUMNS = [
    "order_id", "payment_ref", "expected_amount", "currency",
    "created_at", "status",
]

BREAK_TYPES = [
    "matched", "fee_mismatch", "amount_mismatch", "late_settlement",
    "missing_in_ledger", "missing_in_psp", "duplicate",
]


st.set_page_config(
    page_title="Reconcile-Ops",
    page_icon="  ",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Guarded imports — never let a missing dependency crash the page
# ---------------------------------------------------------------------------

def _import_pandas():
    try:
        import pandas as pd  # noqa: WPS433 (intentional lazy import)
        return pd, None
    except Exception as exc:  # pragma: no cover - environment dependent
        return None, exc


def _import_pipeline():
    """Import the reconcile_ops package pieces we need.

    Returns a dict of callables/classes or (None, error) on failure. Kept lazy
    so the dashboard still renders (with an explanatory banner) even before the
    reconciler/kpis modules exist or if their deps aren't installed.
    """
    try:
        from reconcile_ops.reconciler import Reconciler  # noqa: WPS433
        from reconcile_ops.kpis import compute_kpis  # noqa: WPS433
        return {"Reconciler": Reconciler, "compute_kpis": compute_kpis}, None
    except Exception as exc:  # pragma: no cover - environment dependent
        return None, exc


PD, PANDAS_ERR = _import_pandas()
PIPELINE, PIPELINE_ERR = _import_pipeline()


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def _read_csv(source):
    """Read a CSV from a path or an uploaded file-like object into a DataFrame."""
    if PD is None:
        return None
    return PD.read_csv(source)


@st.cache_data(show_spinner=False)
def _load_sample():
    """Load the bundled sample psp/ledger/ground_truth if present."""
    if PD is None:
        return None, None, None
    psp = _read_csv(DEFAULT_PSP) if DEFAULT_PSP.exists() else None
    ledger = _read_csv(DEFAULT_LEDGER) if DEFAULT_LEDGER.exists() else None
    gt = _read_csv(DEFAULT_GROUND_TRUTH) if DEFAULT_GROUND_TRUTH.exists() else None
    return psp, ledger, gt


def _run_reconciliation(psp_df, ledger_df, gt_df):
    """Run the reconciler + KPIs. Returns (recon_result, kpis, error_or_None)."""
    if PIPELINE is None:
        return None, None, PIPELINE_ERR
    try:
        reconciler = PIPELINE["Reconciler"](psp_df, ledger_df)
        result = reconciler.reconcile()
        kpis = PIPELINE["compute_kpis"](result, psp_df, ledger_df, gt_df)
        return result, kpis, None
    except Exception as exc:  # pragma: no cover - defensive
        return None, None, exc


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

def _fmt_pct(x):
    if x is None:
        return "—"
    try:
        # Accept either a 0-1 fraction or an already-scaled percentage.
        val = float(x)
        if val <= 1.0:
            val *= 100.0
        return f"{val:.1f}%"
    except (TypeError, ValueError):
        return str(x)


def _fmt_money(x):
    if x is None:
        return "—"
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_hours(x):
    if x is None:
        return "—"
    try:
        return f"{float(x):.1f} h"
    except (TypeError, ValueError):
        return str(x)


def _kpi_get(kpis, *keys, default=None):
    """Fetch the first present key from a KPI dict (tolerant of naming drift)."""
    if not isinstance(kpis, dict):
        return default
    for key in keys:
        if key in kpis and kpis[key] is not None:
            return kpis[key]
    return default


def _find_col(df, *candidates):
    """Return the first candidate column that exists in df, else None."""
    if df is None:
        return None
    for col in candidates:
        if col in df.columns:
            return col
    return None


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def render_header():
    st.title("Reconcile-Ops")
    st.caption(
        "PSP-vs-ledger payments reconciliation and KPI analytics. "
        "Every settlement is matched against the internal ledger; unmatched or "
        "off-amount rows become classified exceptions with money quantified at risk."
    )


def render_kpi_cards(kpis):
    st.subheader("KPIs")
    if not isinstance(kpis, dict):
        st.info("KPIs unavailable — run the pipeline to populate these cards.")
        return

    row1 = st.columns(3)
    row1[0].metric(
        "Match rate",
        _fmt_pct(_kpi_get(kpis, "match_rate")),
        help="Share of settlements that reconciled cleanly against the ledger.",
    )
    row1[1].metric(
        "Break rate",
        _fmt_pct(_kpi_get(kpis, "break_rate")),
        help="Share of settlements flagged as an exception of any kind.",
    )
    row1[2].metric(
        "Settlement success",
        _fmt_pct(_kpi_get(kpis, "settlement_success_rate")),
        help="Share of ledger orders that reached a successful settlement.",
    )

    row2 = st.columns(3)
    row2[0].metric(
        "Time to settle (p95)",
        _fmt_hours(_kpi_get(kpis, "time_to_settle_p95", "time_to_settle_p95_hours")),
        help="95th-percentile hours from ledger creation to PSP settlement.",
    )
    row2[1].metric(
        "Cost per txn",
        _fmt_money(_kpi_get(kpis, "cost_per_txn", "avg_fee")),
        help="Average PSP processing fee per settlement.",
    )
    row2[2].metric(
        "Value at risk",
        _fmt_money(_kpi_get(kpis, "value_at_risk")),
        help="Total absolute amount tied up in flagged breaks.",
    )

    precision = _kpi_get(kpis, "precision", "matcher_precision")
    recall = _kpi_get(kpis, "recall", "matcher_recall")
    if precision is not None or recall is not None:
        st.caption(
            f"Matcher vs ground truth — precision **{_fmt_pct(precision)}**, "
            f"recall **{_fmt_pct(recall)}** "
            "(possible because the sample data is synthetic with known labels)."
        )


def _breaks_dataframe(recon_result):
    """Extract the breaks DataFrame from a ReconResult, tolerant of shape."""
    if recon_result is None:
        return None
    breaks = getattr(recon_result, "breaks_df", None)
    if breaks is None and isinstance(recon_result, dict):
        breaks = recon_result.get("breaks_df")
    return breaks


def render_breaks_table(recon_result):
    st.subheader("Breaks")
    breaks = _breaks_dataframe(recon_result)
    if breaks is None or (hasattr(breaks, "empty") and breaks.empty):
        st.success("No breaks to show — everything reconciled (or the pipeline "
                   "has not been run yet).")
        return

    type_col = _find_col(breaks, "break_type", "label")
    if type_col is not None:
        present = sorted(str(v) for v in breaks[type_col].dropna().unique())
        options = [t for t in BREAK_TYPES if t in present]
        # include any unexpected values so nothing is silently hidden
        options += [t for t in present if t not in options]
        selected = st.multiselect(
            "Filter by break type",
            options=options,
            default=options,
            help="Work one exception queue at a time.",
        )
        view = breaks[breaks[type_col].astype(str).isin(selected)] if selected else breaks

        # Per-type counts give an at-a-glance triage picture.
        counts = breaks[type_col].value_counts()
        cols = st.columns(max(1, len(options)))
        for i, t in enumerate(options):
            cols[i % len(cols)].metric(t, int(counts.get(t, 0)))
    else:
        st.caption("Breaks table has no break_type column; showing all rows.")
        view = breaks

    st.dataframe(view, use_container_width=True, hide_index=True)
    _download_button(view, "breaks.csv", "Download filtered breaks (CSV)")


def render_settlement_trend(psp_df):
    st.subheader("Settlement trend over time")
    if psp_df is None or PD is None:
        st.info("Settlement trend needs the PSP settlement data.")
        return

    date_col = _find_col(psp_df, "settled_at")
    amount_col = _find_col(psp_df, "net_amount", "gross_amount")
    if date_col is None or amount_col is None:
        st.info("PSP data is missing a settled_at / amount column.")
        return

    try:
        df = psp_df[[date_col, amount_col]].copy()
        df[date_col] = PD.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col])
        if df.empty:
            st.info("No parseable settlement dates to chart.")
            return
        daily = (
            df.set_index(date_col)
            .resample("D")[amount_col]
            .sum()
            .rename("settled_value")
        )
        st.line_chart(daily, use_container_width=True)
        st.caption(
            f"Daily settled value (sum of `{amount_col}`), "
            f"{daily.index.min().date()} → {daily.index.max().date()}."
        )
    except Exception as exc:  # pragma: no cover - defensive
        st.info(f"Could not build settlement trend: {exc}")


def _download_button(df, filename, label):
    if df is None or PD is None:
        return
    try:
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        st.download_button(label, data=csv_bytes, file_name=filename, mime="text/csv")
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Sidebar: data source selection
# ---------------------------------------------------------------------------

def render_sidebar():
    st.sidebar.header("Data source")
    mode = st.sidebar.radio(
        "Choose input",
        ["Bundled sample data", "Upload my own CSVs"],
        help="The sample is synthetic (ships with ground-truth labels). Upload "
             "your own PSP + ledger exports to reconcile real data.",
    )

    if mode == "Upload my own CSVs":
        st.sidebar.caption(
            "PSP columns: " + ", ".join(PSP_COLUMNS)
        )
        st.sidebar.caption(
            "Ledger columns: " + ", ".join(LEDGER_COLUMNS)
        )
        psp_up = st.sidebar.file_uploader("PSP settlement export (CSV)", type="csv")
        ledger_up = st.sidebar.file_uploader("Internal ledger (CSV)", type="csv")
        gt_up = st.sidebar.file_uploader(
            "Ground truth (optional, CSV)", type="csv",
            help="Only needed for precision/recall scoring.",
        )
        psp = _read_csv(psp_up) if psp_up is not None else None
        ledger = _read_csv(ledger_up) if ledger_up is not None else None
        gt = _read_csv(gt_up) if gt_up is not None else None
        return psp, ledger, gt, mode

    psp, ledger, gt = _load_sample()
    return psp, ledger, gt, mode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    render_header()

    # Hard-stop only if pandas itself is missing — without it nothing renders.
    if PD is None:
        st.error(
            "**pandas is not installed.** Install the project requirements and "
            "relaunch:\n\n"
            "```\nmake setup\nmake app\n```\n\n"
            f"Import error: `{PANDAS_ERR}`"
        )
        return

    psp_df, ledger_df, gt_df, mode = render_sidebar()

    # Data-availability banners.
    if psp_df is None or ledger_df is None:
        if mode == "Bundled sample data":
            st.warning(
                "Sample data not found. Generate it first:\n\n"
                "```\nmake data\n```\n\n"
                "This writes `data/psp.csv`, `data/ledger.csv`, and "
                "`data/ground_truth.csv`."
            )
        else:
            st.info("Upload both a **PSP** and a **ledger** CSV in the sidebar to begin.")
        return

    if PIPELINE is None:
        st.error(
            "The `reconcile_ops` reconciler/KPI modules could not be imported, so "
            "live reconciliation is unavailable. The raw inputs are shown below.\n\n"
            f"Import error: `{PIPELINE_ERR}`"
        )
        with st.expander("PSP settlements (raw)"):
            st.dataframe(psp_df, use_container_width=True, hide_index=True)
        with st.expander("Ledger (raw)"):
            st.dataframe(ledger_df, use_container_width=True, hide_index=True)
        render_settlement_trend(psp_df)
        return

    with st.spinner("Reconciling…"):
        recon_result, kpis, err = _run_reconciliation(psp_df, ledger_df, gt_df)

    if err is not None:
        st.error(f"Reconciliation failed: `{err}`")
        return

    render_kpi_cards(kpis)
    st.divider()
    render_breaks_table(recon_result)
    st.divider()
    render_settlement_trend(psp_df)

    # Optional: expose the raw KPI dict for the curious / for debugging.
    with st.expander("Raw KPIs (JSON)"):
        try:
            st.code(json.dumps(kpis, indent=2, default=str), language="json")
        except Exception:
            st.write(kpis)

    st.caption(
        "Data is synthetic by design — that is what makes ground-truth "
        "precision/recall possible. See the README for the honesty note."
    )


if __name__ == "__main__":
    main()
else:
    # Streamlit executes the module top-to-bottom without __main__, so call main
    # unconditionally when run under `streamlit run`.
    main()
