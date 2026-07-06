.DEFAULT_GOAL := help

IMAGE ?= ghcr.io/rennf93/discord-vexa-bridge
TAG ?= latest

.PHONY: install
install: ## Install runtime + dev deps into a local venv (uv)
	uv sync --extra dev

.PHONY: lock
lock: ## Update the uv lockfile
	uv lock

.PHONY: lint
lint: ## Run ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

.PHONY: fix
fix: ## Auto-fix lint + format
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: typecheck
typecheck: ## Run mypy
	uv run mypy bot.py dave_voice summarizer

.PHONY: security
security: ## Run bandit security scan
	uv run bandit -r bot.py dave_voice summarizer -ll

.PHONY: test
test: ## Run the unit test suite
	uv run pytest -v

.PHONY: check-all
check-all: lint typecheck security test ## Lint, typecheck, security, and test

.PHONY: pre-commit
pre-commit: ## Run all pre-commit hooks
	uv run pre-commit run --all-files

.PHONY: build
build: ## Build the bot Docker image
	docker build -t $(IMAGE):$(TAG) .

.PHONY: run
run: ## Run the bot via docker compose (expects your compose with this service)
	docker compose up -d discord-vexa-bridge

.PHONY: logs
logs: ## Tail the bridge logs
	docker compose logs -f discord-vexa-bridge

.PHONY: clean
clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache **/__pycache__

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'
