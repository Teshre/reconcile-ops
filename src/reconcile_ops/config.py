"""Runtime configuration for the reconciliation engine.

A single immutable :class:`ReconConfig` carries every tunable the matcher and
KPI layer need. Defaults mirror the data generator exactly (SLA = 48h, amount
tolerance = 0.01, fee model = 2.9% + 0.30) so the injected breaks line up with
what the reconciler flags. Values can be overridden programmatically or from the
environment (``RECON_*`` variables) for the CLI / Streamlit app.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Column contracts — the shared data contract. Every component reads/writes
# these exact names. Kept here so modules import them instead of hard-coding
# strings in multiple places.
PSP_COLUMNS = (
    "settlement_id",
    "reference",
    "gross_amount",
    "fee",
    "net_amount",
    "currency",
    "settled_at",
    "status",
)
LEDGER_COLUMNS = (
    "order_id",
    "payment_ref",
    "expected_amount",
    "currency",
    "created_at",
    "status",
)
GROUND_TRUTH_COLUMNS = ("order_id", "settlement_id", "label")


@dataclass(frozen=True)
class ReconConfig:
    """Tunables for the layered matcher.

    Attributes
    ----------
    amount_tolerance:
        Absolute currency tolerance when comparing two amounts. Differences at
        or below this are treated as equal (default ``0.01`` — one cent).
    sla_hours:
        Settlement SLA. A settlement landing more than this many hours after the
        ledger ``created_at`` is a ``late_settlement`` (default ``48``).
    fee_rate:
        Expected proportional PSP fee (default ``0.029`` — 2.9%).
    fee_fixed:
        Expected fixed PSP fee component (default ``0.30``).
    fee_tolerance:
        Absolute slack allowed on top of the modelled fee before a settlement is
        flagged as ``fee_mismatch`` (default ``0.50``). The stated fee may exceed
        the modelled fee by up to this amount and still be considered clean; this
        absorbs cent-level rounding in the generator's fee model.
    """

    amount_tolerance: float = 0.01
    sla_hours: float = 48.0
    fee_rate: float = 0.029
    fee_fixed: float = 0.30
    fee_tolerance: float = 0.50

    # -- derived helpers ---------------------------------------------------
    def expected_fee(self, gross: float) -> float:
        """Modelled plausible PSP fee for a given gross amount."""
        return gross * self.fee_rate + self.fee_fixed

    def amounts_equal(self, a: float, b: float) -> bool:
        """True when two amounts agree within :attr:`amount_tolerance`."""
        return abs(a - b) <= self.amount_tolerance

    @classmethod
    def from_env(cls, **overrides: float) -> "ReconConfig":
        """Build a config from ``RECON_*`` env vars, then apply keyword overrides.

        Recognised variables:
          * ``RECON_AMOUNT_TOLERANCE``
          * ``RECON_SLA_HOURS``
          * ``RECON_FEE_RATE``
          * ``RECON_FEE_FIXED``
          * ``RECON_FEE_TOLERANCE``

        Unset variables fall back to the dataclass defaults. Explicit keyword
        ``overrides`` (e.g. from CLI flags) win over the environment.
        """
        env_map = {
            "amount_tolerance": "RECON_AMOUNT_TOLERANCE",
            "sla_hours": "RECON_SLA_HOURS",
            "fee_rate": "RECON_FEE_RATE",
            "fee_fixed": "RECON_FEE_FIXED",
            "fee_tolerance": "RECON_FEE_TOLERANCE",
        }
        values: dict[str, float] = {}
        for field, env_name in env_map.items():
            raw = os.environ.get(env_name)
            if raw is not None and raw != "":
                try:
                    values[field] = float(raw)
                except ValueError as exc:  # pragma: no cover - defensive
                    raise ValueError(
                        f"{env_name}={raw!r} is not a valid float"
                    ) from exc
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


#: Ready-to-use default configuration matching the generator's assumptions.
DEFAULT_CONFIG = ReconConfig()
