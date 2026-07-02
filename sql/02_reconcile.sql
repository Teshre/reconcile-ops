-- =============================================================================
-- 02_reconcile.sql  —  Reconcile-Ops : the reconciliation model (CENTREPIECE)
-- -----------------------------------------------------------------------------
-- Depends on: 01_load.sql (views psp, ledger, ground_truth).
--
-- What this produces:
--   A single materialised view `reconciliation` with ONE ROW PER RECONCILIATION
--   UNIT and a `break_type` column drawn from the fixed taxonomy:
--
--       matched | fee_mismatch | amount_mismatch | late_settlement
--       missing_in_ledger | missing_in_psp | duplicate
--
--   A "reconciliation unit" is:
--     - every PSP settlement leg (each row of psp, including -DUP legs), joined
--       to its ledger row when the reference matches; PLUS
--     - every ledger row whose payment_ref has NO PSP settlement at all
--       (these surface as missing_in_psp).
--
-- =============================================================================
-- HOW THE CLASSIFIER WORKS (layered, first-match-wins precedence)
-- -----------------------------------------------------------------------------
-- The contract defines a strict precedence so each unit gets exactly one label.
-- We evaluate the layers top-to-bottom; the first layer that fires wins:
--
--   L0. STRUCTURAL — reference exists on only one side:
--         * PSP reference with no ledger row      -> missing_in_ledger
--         * ledger payment_ref with no PSP row     -> missing_in_psp
--       (These short-circuit everything else: with no counterpart there is no
--        amount/date to compare.)
--
--   L1. DUPLICATE — the reference appears on >= 2 PSP settlement legs.
--       The generator labels the ORIGINAL leg as `matched` and the EXTRA
--       leg (settlement_id ending in '-DUP') as `duplicate`. We mirror that:
--       only the surplus leg(s) — every leg beyond the first, ranked so the
--       -DUP suffix sorts last — carry `duplicate`. The first/original leg
--       continues down the layers and is judged on its own merits.
--
--   L2. AMOUNT — compare the GROSS side:  |gross_amount - expected_amount|.
--       expected_amount is the GROSS billed amount (contract note), so on a
--       clean deal gross ≈ expected. If they diverge beyond tolerance the
--       principal itself is wrong  ->  amount_mismatch.
--
--   L3. FEE — the gross side reconciles, but the stated PSP `fee` is larger
--       than any plausible fee (standard card economics ≈ 2.9% + $0.30).
--       When fee is implausibly high, net is correspondingly too low even
--       though gross == expected  ->  fee_mismatch.  (Because L2 already
--       passed, amount is fine; only the fee/net split is off.)
--
--   L4. TIMELINESS — amounts and fee are clean, but the settlement breached
--       the SLA: settled_at - created_at > SLA_HOURS  ->  late_settlement.
--
--   L5. MATCHED — nothing above fired: gross ≈ expected, fee is plausible,
--       and it settled within SLA. A clean reconciliation.
--
-- TUNABLES (generator defaults — keep in sync with the Python reconciler):
--   * AMOUNT_TOLERANCE = 0.01   absolute currency tolerance on gross vs expected
--   * SLA_HOURS        = 48     max hours from ledger.created_at to settled_at
--   * FEE_RATE = 0.029, FEE_FIXED = 0.30, FEE_TOLERANCE = 0.05
--       plausible_fee = FEE_RATE * gross + FEE_FIXED ; a fee above
--       plausible_fee + FEE_TOLERANCE is treated as a fee_mismatch.
--
-- NOTE ON PORTABILITY: DuckDB has no session variables in a plain .read script,
-- so the tunables are inlined as literals below and documented here. Change
-- them in one place (the CASE expression) if the generator defaults change.
-- =============================================================================

CREATE OR REPLACE VIEW reconciliation AS
WITH
-- ---------------------------------------------------------------------------
-- ref_counts: how many PSP legs share each reference. > 1 => duplicate family.
-- ---------------------------------------------------------------------------
ref_counts AS (
    SELECT reference, count(*) AS n_legs
    FROM psp
    GROUP BY reference
),

-- ---------------------------------------------------------------------------
-- psp_ranked: rank the legs within a duplicate family so the "surplus" legs
-- can be flagged. We order so that a settlement_id containing '-DUP' sorts
-- LAST (rn > 1). The first leg (rn = 1) is the original and is reconciled
-- normally; every leg with rn > 1 is a duplicate.
-- ---------------------------------------------------------------------------
psp_ranked AS (
    SELECT
        p.*,
        rc.n_legs,
        ROW_NUMBER() OVER (
            PARTITION BY p.reference
            ORDER BY
                CASE WHEN p.settlement_id LIKE '%-DUP' THEN 1 ELSE 0 END,  -- originals first
                p.settlement_id                                            -- stable tiebreak
        ) AS leg_rank
    FROM psp p
    JOIN ref_counts rc USING (reference)
),

-- ---------------------------------------------------------------------------
-- psp_join: LEFT JOIN each PSP leg to its ledger counterpart on the match key.
-- A NULL ledger side after this join means the PSP reference is orphaned
-- (missing_in_ledger). We compute all comparison scalars here so the CASE
-- ladder below reads cleanly.
-- ---------------------------------------------------------------------------
psp_join AS (
    SELECT
        pr.settlement_id,
        pr.reference,
        l.order_id,
        pr.gross_amount,
        pr.fee,
        pr.net_amount,
        l.expected_amount,
        pr.currency          AS psp_currency,
        l.currency           AS ledger_currency,
        pr.settled_at,
        l.created_at,
        pr.status            AS psp_status,
        l.status             AS ledger_status,
        pr.n_legs,
        pr.leg_rank,
        (l.payment_ref IS NULL)                       AS ledger_missing,
        -- Amount divergence on the GROSS side (principal check):
        abs(pr.gross_amount - l.expected_amount)      AS gross_gap,
        -- Fee plausibility: how far the stated fee exceeds standard economics:
        pr.fee - (0.029 * pr.gross_amount + 0.30)     AS fee_excess,
        -- Settlement latency in hours (fractional). NULL when unmatched.
        CASE
            WHEN l.created_at IS NULL THEN NULL
            ELSE date_diff('minute', l.created_at, pr.settled_at) / 60.0
        END                                            AS settle_hours
    FROM psp_ranked pr
    LEFT JOIN ledger l
           ON pr.reference = l.payment_ref
),

-- ---------------------------------------------------------------------------
-- psp_side: apply the layered classifier (L0–L5) to every PSP leg.
-- ---------------------------------------------------------------------------
psp_side AS (
    SELECT
        settlement_id,
        reference,
        order_id,
        gross_amount,
        fee,
        net_amount,
        expected_amount,
        psp_currency        AS currency,
        settled_at,
        created_at,
        psp_status,
        ledger_status,
        gross_gap,
        settle_hours,
        CASE
            -- L0: PSP reference has no ledger counterpart at all.
            WHEN ledger_missing
                THEN 'missing_in_ledger'
            -- L1: this leg is a surplus copy within a duplicate family.
            WHEN n_legs > 1 AND leg_rank > 1
                THEN 'duplicate'
            -- L2: gross principal disagrees beyond tolerance.
            WHEN gross_gap > 0.01
                THEN 'amount_mismatch'
            -- L3: gross reconciles but the fee is implausibly high.
            WHEN fee_excess > 0.05
                THEN 'fee_mismatch'
            -- L4: amounts clean but settlement breached the 48h SLA.
            WHEN settle_hours > 48
                THEN 'late_settlement'
            -- L5: clean match.
            ELSE 'matched'
        END AS break_type
    FROM psp_join
),

-- ---------------------------------------------------------------------------
-- ledger_only: ledger rows whose payment_ref never appears in PSP. These are
-- captures we expected to settle but the PSP never reported => missing_in_psp.
-- We emit them as first-class reconciliation units (with NULL PSP fields).
-- ---------------------------------------------------------------------------
ledger_only AS (
    SELECT
        CAST(NULL AS VARCHAR)   AS settlement_id,
        l.payment_ref           AS reference,
        l.order_id,
        CAST(NULL AS DOUBLE)    AS gross_amount,
        CAST(NULL AS DOUBLE)    AS fee,
        CAST(NULL AS DOUBLE)    AS net_amount,
        l.expected_amount,
        l.currency,
        CAST(NULL AS TIMESTAMP) AS settled_at,
        l.created_at,
        CAST(NULL AS VARCHAR)   AS psp_status,
        l.status                AS ledger_status,
        CAST(NULL AS DOUBLE)    AS gross_gap,
        CAST(NULL AS DOUBLE)    AS settle_hours,
        'missing_in_psp'        AS break_type
    FROM ledger l
    WHERE NOT EXISTS (
        SELECT 1 FROM psp p WHERE p.reference = l.payment_ref
    )
)

-- ---------------------------------------------------------------------------
-- Unified reconciliation table: every PSP leg + every ledger-only orphan.
-- `is_break` is a convenience flag (anything that is not a clean match).
-- ---------------------------------------------------------------------------
SELECT
    settlement_id,
    reference,
    order_id,
    gross_amount,
    fee,
    net_amount,
    expected_amount,
    currency,
    settled_at,
    created_at,
    psp_status,
    ledger_status,
    gross_gap,
    settle_hours,
    break_type,
    (break_type <> 'matched') AS is_break
FROM psp_side

UNION ALL

SELECT
    settlement_id,
    reference,
    order_id,
    gross_amount,
    fee,
    net_amount,
    expected_amount,
    currency,
    settled_at,
    created_at,
    psp_status,
    ledger_status,
    gross_gap,
    settle_hours,
    break_type,
    TRUE AS is_break
FROM ledger_only;

-- ---------------------------------------------------------------------------
-- Convenience projection: the break_type distribution of the model output.
-- Mirrors the shape of ground_truth's label distribution for eyeballing.
-- ---------------------------------------------------------------------------
SELECT
    break_type,
    count(*)                                                   AS n,
    round(100.0 * count(*) / SUM(count(*)) OVER (), 2)         AS pct
FROM reconciliation
GROUP BY break_type
ORDER BY n DESC;
