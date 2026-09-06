# capwrap — development tasks
# Run `make` or `make help` to list targets.

PYTHON   := .venv/bin/python
PIP      := $(PYTHON) -m pip
RUFF     := $(shell [ -x .venv/bin/ruff ] && echo .venv/bin/ruff || echo ruff)
MYPY     := $(shell [ -x .venv/bin/mypy ] && echo .venv/bin/mypy || echo mypy)
PRETTIER := npx --yes prettier@3

# Frontend sources: console static assets + guest-side TS plugins.
# Vendored libs (xterm) are excluded via .prettierignore.
FRONTEND_PATHS := capwrap/web/static capwrap/guest/opencode-plugin.ts capwrap/guest/pi-extension.ts

.DEFAULT_GOAL := help
.PHONY: help venv install install-dev lint lint-fix format format-backend format-frontend \
        format-check typecheck typecheck-pyright test check ci clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

venv: ## Create .venv if it does not exist
	@test -x $(PYTHON) || python3 -m venv .venv

install: venv ## Install capwrap into .venv
	$(PIP) install -e .

install-dev: venv ## Install capwrap + dev tools (pytest, ruff, mypy)
	$(PIP) install -e '.[dev]'

lint: ## Lint with ruff (report only)
	$(RUFF) check .

lint-fix: ## Lint with ruff, applying safe autofixes
	$(RUFF) check --fix .

format: format-backend format-frontend ## Format backend and frontend

format-backend: ## Format Python with ruff
	$(RUFF) format .

format-frontend: ## Format JS/CSS/HTML/TS with prettier (fetched via npx)
	$(PRETTIER) --write --no-error-on-unmatched-pattern $(FRONTEND_PATHS)

format-check: ## Verify formatting without modifying files
	$(RUFF) format --check .
	$(PRETTIER) --check --no-error-on-unmatched-pattern $(FRONTEND_PATHS)

typecheck: ## Static type checks with mypy
	$(MYPY) capwrap

typecheck-pyright: ## Static type checks with pyright (alternative to mypy)
	pyright capwrap

test: ## Run the test suite (sandbox tests skip when bwrap is unavailable)
	$(PYTHON) -m pytest

check: lint format-check typecheck ## All static checks

ci: check test ## Static checks + tests

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build *.egg-info
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
