-- =============================================================================
-- 03_kpis.sql  —  Reconcile-Ops : KPI model
-- -----------------------------------------------------------------------------
-- Depends on: 01_load.sql (views) and 02_reconcile.sql (view `reconciliation`).
--
-- Produces a SINGLE-ROW result set with every KPI in the contract:
--
--   match_rate                 share of reconciliation units that are `matched`
--   break_rate                 1 - match_rate (share that are breaks)
--   settlement_success_rate    share of PSP settlements with status='settled'
--   time_to_settle_p50_hours   median settlement latency (matched+timing units)
--   time_to_settle_p95_hours   95th-percentile settlement latency
--   cost_per_txn               average PSP fee per settlement
--   value_at_risk              sum of |amount| tied up in break rows
--   matcher_precision          precision of the matcher vs ground_truth
--   matcher_recall             recall of the matcher vs ground_truth
--
-- -----------------------------------------------------------------------------
-- PRECISION / RECALL DEFINITION
--   We evaluate the matcher as a MULTICLASS labeller against ground_truth,
--   then report MICRO-averaged precision and recall over the break classes
--   (i.e. treating "matched" as the negative class). Concretely:
--
--     TP = a unit the model calls a break AND ground_truth agrees it is a
--          break, with the SAME break_type  (correct break, correct kind)
--     FP = model calls it a break but ground_truth says matched, OR the model
--          picked the wrong break_type
--     FN = ground_truth says it is a break but the model called it matched
--
--     precision = TP / (TP + FP)      recall = TP / (TP + FN)
--
--   Join key to ground_truth:
--     * settlement_id when present (PSP-side units, incl. -DUP legs), else
--     * order_id (ledger-only `missing_in_psp` units, which have no
--       settlement_id).
-- =============================================================================

WITH
-- ---------------------------------------------------------------------------
-- recon: the model output, plus a single join key `gt_key` used to line each
-- unit up with its ground_truth label.
-- ---------------------------------------------------------------------------
recon AS (
    SELECT
        r.*,
        COALESCE(r.settlement_id, r.order_id) AS gt_key
    FROM reconciliation r
),

-- ---------------------------------------------------------------------------
-- gt: ground truth with the same COALESCE key so we can join on one column.
-- ---------------------------------------------------------------------------
gt AS (
    SELECT
        COALESCE(settlement_id, order_id) AS gt_key,
        label
    FROM ground_truth
),

-- ---------------------------------------------------------------------------
-- scored: align each model unit with its true label.
-- ---------------------------------------------------------------------------
scored AS (
    SELECT
        recon.gt_key,
        recon.break_type       AS pred_label,
        gt.label               AS true_label,
        recon.is_break         AS pred_is_break,
        (gt.label <> 'matched') AS true_is_break
    FROM recon
    LEFT JOIN gt USING (gt_key)
),

-- ---------------------------------------------------------------------------
-- confusion: micro TP / FP / FN counts over the break classes.
-- ---------------------------------------------------------------------------
confusion AS (
    SELECT
        -- correct break, correct kind
        SUM(CASE WHEN pred_is_break AND true_is_break
                  AND pred_label = true_label THEN 1 ELSE 0 END) AS tp,
        -- flagged a break that either wasn't one, or was the wrong kind
        SUM(CASE WHEN pred_is_break
                  AND (NOT true_is_break OR pred_label <> true_label)
                 THEN 1 ELSE 0 END)                              AS fp,
        -- missed a real break (called it matched)
        SUM(CASE WHEN NOT pred_is_break AND true_is_break
                 THEN 1 ELSE 0 END)                              AS fn
    FROM scored
),

-- ---------------------------------------------------------------------------
-- recon_stats: the operational KPIs computed straight off the model output.
-- ---------------------------------------------------------------------------
recon_stats AS (
    SELECT
        count(*)                                                     AS n_units,
        SUM(CASE WHEN break_type = 'matched' THEN 1 ELSE 0 END)      AS n_matched,
        SUM(CASE WHEN is_break THEN 1 ELSE 0 END)                    AS n_breaks,
        -- p50 / p95 latency over units that actually have a settlement time.
        quantile_cont(settle_hours, 0.50)
            FILTER (WHERE settle_hours IS NOT NULL)                  AS p50_hours,
        quantile_cont(settle_hours, 0.95)
            FILTER (WHERE settle_hours IS NOT NULL)                  AS p95_hours,
        -- value tied up in breaks: prefer the PSP gross, fall back to the
        -- ledger expected amount for missing_in_psp (no PSP row).
        SUM(CASE WHEN is_break
                 THEN abs(COALESCE(gross_amount, expected_amount, 0))
                 ELSE 0 END)                                         AS value_at_risk
    FROM reconciliation
),

-- ---------------------------------------------------------------------------
-- psp_stats: KPIs that are defined over the raw PSP settlement population.
-- ---------------------------------------------------------------------------
psp_stats AS (
    SELECT
        count(*)                                                    AS n_settlements,
        SUM(CASE WHEN status = 'settled' THEN 1 ELSE 0 END)         AS n_settled,
        avg(fee)                                                    AS avg_fee
    FROM psp
)

-- ---------------------------------------------------------------------------
-- Final single-row KPI panel.
-- ---------------------------------------------------------------------------
SELECT
    -- Reconciliation health
    round(rs.n_matched  * 1.0 / rs.n_units, 4)                       AS match_rate,
    round(rs.n_breaks   * 1.0 / rs.n_units, 4)                       AS break_rate,
    round(ps.n_settled  * 1.0 / ps.n_settlements, 4)                 AS settlement_success_rate,

    -- Timeliness (hours)
    round(rs.p50_hours, 2)                                          AS time_to_settle_p50_hours,
    round(rs.p95_hours, 2)                                          AS time_to_settle_p95_hours,

    -- Economics
    round(ps.avg_fee, 4)                                            AS cost_per_txn,
    round(rs.value_at_risk, 2)                                      AS value_at_risk,

    -- Matcher quality vs ground_truth (micro-averaged over break classes)
    round(c.tp * 1.0 / NULLIF(c.tp + c.fp, 0), 4)                   AS matcher_precision,
    round(c.tp * 1.0 / NULLIF(c.tp + c.fn, 0), 4)                   AS matcher_recall,

    -- Raw counts for transparency / debugging
    rs.n_units,
    rs.n_matched,
    rs.n_breaks,
    c.tp                                                            AS eval_tp,
    c.fp                                                            AS eval_fp,
    c.fn                                                            AS eval_fn
FROM recon_stats rs
CROSS JOIN psp_stats  ps
CROSS JOIN confusion  c;
