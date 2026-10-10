.DEFAULT_GOAL := help
.PHONY: help venv install fmt lint type test check run up down clean

PY ?= python3
VENV := .venv
ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
else
BIN := $(VENV)/bin
endif
EXTRAS := dev,ingestion,platform,brain,interface,dashboard

help:  ## List the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv:  ## Create the virtual environment
	$(PY) -m venv $(VENV)

install: venv  ## Install development dependencies
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[$(EXTRAS)]"

fmt:  ## Format the code
	$(BIN)/ruff format src tests scripts
	$(BIN)/ruff check --fix src tests scripts

lint:  ## Run lint checks
	$(BIN)/ruff check src tests scripts
	$(BIN)/ruff format --check src tests scripts

type:  ## Run type checks (mypy)
	$(BIN)/mypy

test:  ## Run the tests
	$(BIN)/pytest

check: lint type test  ## Full quality gate (lint + type + test)

run:  ## Run Argus (health check + version)
	$(BIN)/python -m argus

up:  ## Start the docker-compose stack
	docker compose up --build -d

down:  ## Stop the docker-compose stack
	docker compose down

clean:  ## Remove temporary files
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
