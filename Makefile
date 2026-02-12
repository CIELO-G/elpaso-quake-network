# =============================================================================
# El Paso Seismic Pipeline — Makefile
# =============================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash

PYTHON ?= python
CONDA_ENV ?= elpaso-quake

# ── Installation ─────────────────────────────────────────────────────────────

.PHONY: install
install: ## Create/update Conda environment from environment.yml
	conda env update -f environment.yml -n $(CONDA_ENV) --prune
	@echo "Activate with: conda activate $(CONDA_ENV)"

.PHONY: install-dev
install-dev: install ## Install dev dependencies (ruff, mypy, pytest, pre-commit)
	conda run -n $(CONDA_ENV) pip install -e ".[dev]"
	conda run -n $(CONDA_ENV) pre-commit install

# ── Code Quality ─────────────────────────────────────────────────────────────

.PHONY: lint
lint: ## Run ruff linter
	ruff check .

.PHONY: format
format: ## Auto-format code with ruff
	ruff format .
	ruff check --fix .

.PHONY: typecheck
typecheck: ## Run mypy type checker
	mypy --ignore-missing-imports lib/ run_pipeline.py dashboard/app.py

# ── Testing ──────────────────────────────────────────────────────────────────

.PHONY: test
test: ## Run unit tests with pytest
	pytest tests/ -v --tb=short

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	pytest tests/ -v --tb=short --cov=lib --cov=dashboard --cov-report=term-missing

# ── Pipeline ─────────────────────────────────────────────────────────────────

.PHONY: run
run: ## Run pipeline (single day). Usage: make run START=2026-01-15 END=2026-01-15
	$(PYTHON) run_pipeline.py --start $(START) --end $(END)

.PHONY: run-continuous
run-continuous: ## Run pipeline in continuous mode
	$(PYTHON) run_pipeline.py --continuous

.PHONY: validate
validate: ## Validate pipeline environment (FDSNWS, disk, config)
	$(PYTHON) run_pipeline.py --validate

.PHONY: dashboard
dashboard: ## Start the FastAPI dashboard on port 8050
	$(PYTHON) -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8050 --reload

# ── Docker ───────────────────────────────────────────────────────────────────

.PHONY: docker-build
docker-build: ## Build Docker image
	docker build -t elpaso-quake .

.PHONY: docker-up
docker-up: ## Start all services with docker compose
	docker compose up -d

.PHONY: docker-down
docker-down: ## Stop all services
	docker compose down

# ── Data Management ──────────────────────────────────────────────────────────

.PHONY: backup
backup: ## Backup catalog and database
	$(PYTHON) scripts/backup.py

.PHONY: clean-old-data
clean-old-data: ## Delete raw waveforms older than RETENTION_DAYS (default: 90)
	$(PYTHON) scripts/data_retention.py

# ── Housekeeping ─────────────────────────────────────────────────────────────

.PHONY: clean
clean: ## Remove Python caches and temporary files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type f -name '*.pyc' -delete 2>/dev/null; true
	find . -type f -name '*.pyo' -delete 2>/dev/null; true
	rm -rf .mypy_cache .pytest_cache .ruff_cache dist build *.egg-info

# ── Help ─────────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
