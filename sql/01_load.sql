-- =============================================================================
-- 01_load.sql  —  Reconcile-Ops : data loading layer
-- -----------------------------------------------------------------------------
-- Purpose:
--   Create typed VIEWS over the three source CSVs so that the reconciliation
--   model (02_reconcile.sql) and the KPI model (03_kpis.sql) can query stable,
--   well-named relations instead of re-parsing files inline.
--
-- Execution:
--   duckdb -c ".read sql/01_load.sql" -c ".read sql/02_reconcile.sql" \
--          -c ".read sql/03_kpis.sql"
--   (run from the repo root so the relative data/*.csv paths resolve)
--
-- Design notes:
--   * We use read_csv_auto with header=true so DuckDB infers the numeric and
--     timestamp types directly from the header contract. The generator writes
--     naive ISO-8601 datetimes (YYYY-MM-DDTHH:MM:SS, UTC-assumed), which
--     DuckDB parses into TIMESTAMP without a timezone — exactly what we want.
--   * nullstr='' is CRITICAL for ground_truth.csv: the generator leaves
--     order_id blank for `missing_in_ledger` rows and settlement_id blank for
--     `missing_in_psp` rows. Treating '' as NULL keeps those joins honest.
--   * These are VIEWS (not tables): zero-copy, always reflect the current CSV
--     on disk, and cost nothing to (re)create. Re-running this script is safe.
-- =============================================================================

-- The column contract (must match the CSV headers verbatim):
--   psp.csv      : settlement_id, reference, gross_amount, fee, net_amount,
--                  currency, settled_at, status
--   ledger.csv   : order_id, payment_ref, expected_amount, currency,
--                  created_at, status
--   ground_truth : order_id, settlement_id, label

CREATE OR REPLACE VIEW psp AS
SELECT
    settlement_id,          -- unique PSP settlement id (dup legs carry a -DUP suffix)
    reference,              -- the MATCH KEY: joins to ledger.payment_ref
    gross_amount,           -- gross charge the customer was billed (== ledger.expected on a clean match)
    fee,                    -- PSP processing fee (gross - net should equal this)
    net_amount,             -- amount actually settled to the merchant (gross - fee)
    currency,
    CAST(settled_at AS TIMESTAMP) AS settled_at,   -- when the PSP settled the funds
    status
FROM read_csv_auto('data/psp.csv', header = true);

CREATE OR REPLACE VIEW ledger AS
SELECT
    order_id,               -- internal order id
    payment_ref,            -- the MATCH KEY: joins to psp.reference
    expected_amount,        -- GROSS order/charge amount (what we billed) — NOT net
    currency,
    CAST(created_at AS TIMESTAMP) AS created_at,   -- when the order/charge was created
    status
FROM read_csv_auto('data/ledger.csv', header = true);

CREATE OR REPLACE VIEW ground_truth AS
SELECT
    order_id,               -- blank -> NULL for missing_in_ledger rows
    settlement_id,          -- blank -> NULL for missing_in_psp rows
    label                   -- one of the 7 taxonomy strings; the supervised label
FROM read_csv_auto('data/ground_truth.csv', header = true, nullstr = '');

-- Sanity echo: row counts per source. Handy when running interactively; the
-- reconciler/KPI scripts do not depend on this result.
SELECT 'psp'          AS relation, count(*) AS n_rows FROM psp
UNION ALL
SELECT 'ledger'       AS relation, count(*) AS n_rows FROM ledger
UNION ALL
SELECT 'ground_truth' AS relation, count(*) AS n_rows FROM ground_truth
ORDER BY relation;
