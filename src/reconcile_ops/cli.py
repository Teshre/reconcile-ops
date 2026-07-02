"""Command-line entry point for the reconciliation pipeline.

Usage::

    python -m reconcile_ops.cli \
        --psp data/psp.csv \
        --ledger data/ledger.csv \
        --out out/ \
        [--ground-truth data/ground_truth.csv] \
        [--sla-hours 48] [--amount-tolerance 0.01]

Reads the PSP + ledger CSVs, runs the :class:`~reconcile_ops.reconciler.Reconciler`,
computes KPIs, writes ``matched.csv``, ``breaks.csv`` and ``kpis.json`` into the
output directory, and prints a human-readable KPI summary to stdout.

If ``--ground-truth`` is omitted the CLI auto-detects a ``ground_truth.csv`` next
to the PSP file (so the default invocation still scores precision/recall).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import ReconConfig
from .kpis import compute_kpis
from .reconciler import ReconResult, Reconciler


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"input CSV not found: {path}")
    return pd.read_csv(path)


def _write_outputs(result: ReconResult, kpis: dict, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    matched_path = out_dir / "matched.csv"
    breaks_path = out_dir / "breaks.csv"
    kpis_path = out_dir / "kpis.json"

    result.matched_df.to_csv(matched_path, index=False)
    result.breaks_df.to_csv(breaks_path, index=False)
    kpis_path.write_text(json.dumps(kpis, indent=2, default=str), encoding="utf-8")

    return {"matched": matched_path, "breaks": breaks_path, "kpis": kpis_path}


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def _fmt_pct(x: float) -> str:
    return f"{100.0 * float(x):.2f}%"


def format_summary(kpis: dict, paths: Optional[dict] = None) -> str:
    """Render the KPI dict as an aligned text block for the terminal."""
    lines: list[str] = []
    lines.append("=" * 56)
    lines.append("  RECONCILE-OPS — reconciliation summary")
    lines.append("=" * 56)
    lines.append(f"  PSP settlements    : {kpis['n_psp']:>8,}")
    lines.append(f"  Ledger orders      : {kpis['n_ledger']:>8,}")
    lines.append(f"  Matched            : {kpis['n_matched']:>8,}")
    lines.append(f"  Breaks             : {kpis['n_breaks']:>8,}")
    lines.append("-" * 56)
    lines.append(f"  match_rate               : {_fmt_pct(kpis['match_rate']):>10}")
    lines.append(f"  break_rate               : {_fmt_pct(kpis['break_rate']):>10}")
    lines.append(
        f"  settlement_success_rate  : {_fmt_pct(kpis['settlement_success_rate']):>10}"
    )
    lines.append(f"  time_to_settle_p50 (h)   : {kpis['time_to_settle_p50']:>10.2f}")
    lines.append(f"  time_to_settle_p95 (h)   : {kpis['time_to_settle_p95']:>10.2f}")
    lines.append(f"  cost_per_txn             : {kpis['cost_per_txn']:>10.4f}")
    lines.append(f"  value_at_risk            : {kpis['value_at_risk']:>12,.2f}")
    lines.append("-" * 56)
    lines.append("  breaks by type:")
    by_type = kpis.get("breaks_by_type", {})
    if by_type:
        for label in sorted(by_type, key=lambda k: -by_type[k]):
            lines.append(f"    {label:<20} {by_type[label]:>6,}")
    else:
        lines.append("    (none)")

    scoring = kpis.get("scoring")
    if scoring:
        bd = scoring["break_detection"]
        macro = scoring["macro"]
        lines.append("-" * 56)
        lines.append("  vs ground truth:")
        lines.append(
            f"    break-detection  P={_fmt_pct(bd['precision'])}"
            f"  R={_fmt_pct(bd['recall'])}  F1={_fmt_pct(bd['f1'])}"
        )
        lines.append(
            f"    exact-label acc  {_fmt_pct(scoring['exact_label_accuracy'])}"
        )
        lines.append(
            f"    macro            P={_fmt_pct(macro['precision'])}"
            f"  R={_fmt_pct(macro['recall'])}  F1={_fmt_pct(macro['f1'])}"
        )
        lines.append("    per-label (precision / recall / support):")
        for label in sorted(scoring["per_label"]):
            m = scoring["per_label"][label]
            lines.append(
                f"      {label:<18} "
                f"P={_fmt_pct(m['precision']):>8}  "
                f"R={_fmt_pct(m['recall']):>8}  "
                f"n={m['support']:>5}"
            )

    if paths:
        lines.append("-" * 56)
        lines.append("  outputs:")
        for name, p in paths.items():
            lines.append(f"    {name:<10} {p}")
    lines.append("=" * 56)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Argument parsing / orchestration
# ---------------------------------------------------------------------------

def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="reconcile_ops.cli",
        description="Reconcile a PSP settlement export against an internal ledger.",
    )
    p.add_argument("--psp", required=True, type=Path, help="path to psp.csv")
    p.add_argument("--ledger", required=True, type=Path, help="path to ledger.csv")
    p.add_argument("--out", required=True, type=Path, help="output directory")
    p.add_argument(
        "--ground-truth",
        type=Path,
        default=None,
        help="optional ground_truth.csv for precision/recall scoring "
        "(auto-detected next to --psp if present)",
    )
    p.add_argument(
        "--sla-hours",
        type=float,
        default=None,
        help="settlement SLA in hours (default 48; env RECON_SLA_HOURS)",
    )
    p.add_argument(
        "--amount-tolerance",
        type=float,
        default=None,
        help="amount comparison tolerance (default 0.01; env RECON_AMOUNT_TOLERANCE)",
    )
    p.add_argument(
        "--no-score",
        action="store_true",
        help="skip ground-truth scoring even if a ground_truth.csv is found",
    )
    return p.parse_args(argv)


def _resolve_ground_truth(args: argparse.Namespace) -> Optional[Path]:
    if args.no_score:
        return None
    if args.ground_truth is not None:
        return args.ground_truth
    candidate = args.psp.parent / "ground_truth.csv"
    return candidate if candidate.exists() else None


def run(argv=None) -> dict:
    """Run the pipeline and return the KPI dict (importable for tests/app)."""
    args = _parse_args(argv)

    config = ReconConfig.from_env(
        sla_hours=args.sla_hours,
        amount_tolerance=args.amount_tolerance,
    )

    psp_df = _read_csv(args.psp)
    ledger_df = _read_csv(args.ledger)

    result = Reconciler(psp_df, ledger_df, config=config).reconcile()

    gt_path = _resolve_ground_truth(args)
    ground_truth_df = _read_csv(gt_path) if gt_path else None

    kpis = compute_kpis(result, psp_df, ledger_df, ground_truth_df)
    paths = _write_outputs(result, kpis, args.out)

    print(format_summary(kpis, paths))
    return kpis


def main(argv=None) -> int:
    try:
        run(argv)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
