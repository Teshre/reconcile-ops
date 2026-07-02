# Reconcile-Ops — developer entrypoints.
#
# Quickstart:
#     make setup      # create venv + install dependencies
#     make data       # generate the synthetic PSP / ledger / ground-truth CSVs
#     make run        # run the reconciliation pipeline -> out/*.csv + kpis.json
#     make app        # launch the Streamlit dashboard
#
# Every Python target runs inside the local virtualenv (./.venv) and sets
# PYTHONPATH=src so the `reconcile_ops` package is importable without an install.

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PYTHON       ?= python3
VENV         := .venv
BIN          := $(VENV)/bin
VENV_PYTHON  := $(BIN)/python
PIP          := $(BIN)/pip

# Make the package importable from src/ in every target.
export PYTHONPATH := src

# Default I/O locations (override on the command line, e.g. `make run OUT=/tmp/x`).
PSP    ?= data/psp.csv
LEDGER ?= data/ledger.csv
OUT    ?= out
SEED   ?= 42
N      ?= 5000

# Use bash so `source` works if a recipe ever needs it.
SHELL := /bin/bash

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Phony targets
# ---------------------------------------------------------------------------
.PHONY: help setup data run test app sql lint clean distclean

help: ## Show this help.
	@echo "Reconcile-Ops — make targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
$(VENV_PYTHON):
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

setup: $(VENV_PYTHON) ## Create the virtualenv and install dependencies.
	$(PIP) install -r requirements.txt
	@echo "Environment ready. Next: make data && make run && make app"

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
data: ## Generate the seeded synthetic CSVs into data/.
	$(VENV_PYTHON) data/generate.py --n $(N) --seed $(SEED) --outdir data

run: ## Run the reconciliation CLI -> out/matched.csv, out/breaks.csv, out/kpis.json.
	$(VENV_PYTHON) -m reconcile_ops.cli --psp $(PSP) --ledger $(LEDGER) --out $(OUT)/

test: ## Run the test suite.
	$(BIN)/pytest -q

app: ## Launch the Streamlit dashboard.
	$(BIN)/streamlit run app/streamlit_app.py

sql: ## Run the DuckDB reconciliation + KPI SQL over the CSVs.
	$(VENV_PYTHON) -c "import duckdb; con = duckdb.connect(); \
		[con.execute(open(f).read()) for f in ['sql/01_load.sql','sql/02_reconcile.sql']]; \
		print(con.execute(open('sql/03_kpis.sql').read()).df().to_string(index=False))"

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
lint: ## Byte-compile the package + app to catch syntax errors (no extra deps).
	$(VENV_PYTHON) -m compileall -q src app data

clean: ## Remove generated pipeline output and caches (keeps the venv).
	rm -rf $(OUT)/*.csv $(OUT)/*.json
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache

distclean: clean ## Also remove the virtualenv and generated data.
	rm -rf $(VENV) data/psp.csv data/ledger.csv data/ground_truth.csv
