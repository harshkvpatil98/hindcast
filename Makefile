SHELL := /bin/bash
.DEFAULT_GOAL := help

# Load .env so both docker compose and the CLI see the same values.
ifneq (,$(wildcard .env))
include .env
export
endif

UV ?= uv
COMPOSE ?= docker compose
RUN := $(UV) run

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

.PHONY: install
install: ## Create the venv and install what the demo and the test suite need
	# `kernel` (langgraph) is not optional in practice: the simulation kernel
	# imports it, so `make demo`, `cascade simulate` and `cascade trace replay`
	# all need it. `embed` is left out -- it is a multi-gigabyte download that
	# only the corpus and retrieval paths use.
	$(UV) sync --extra dev --extra kernel --extra aws

.PHONY: install-full
install-full: ## Everything, including the embedding stack (torch, ~2GB)
	$(UV) sync --extra dev --extra kernel --extra aws --extra embed --extra analytics

.PHONY: env
env: ## Write a .env with local development defaults if none exists
	@test -f .env || (cp .env.example .env && echo "wrote .env from .env.example")
	@test -f .env && echo ".env present"

# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

.PHONY: up
up: env ## Bring Postgres + Langfuse up and wait for both to be healthy
	$(COMPOSE) up -d --wait
	@echo "postgres: $$($(COMPOSE) ps --format '{{.Name}} {{.Status}}' postgres)"
	@echo "langfuse: $$($(COMPOSE) ps --format '{{.Name}} {{.Status}}' langfuse)"

.PHONY: down
down: ## Stop the stack, keeping the data volume
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop the stack and delete the data volume (destructive)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail service logs
	$(COMPOSE) logs -f --tail=100

.PHONY: migrate
migrate: ## Apply pending SQL migrations
	$(RUN) cascade db migrate

.PHONY: seed
seed: ## Build and seal the scenario registry (M1)
	$(RUN) cascade ledger

# ---------------------------------------------------------------------------
# Quality gates -- all four must be green (spec §13)
# ---------------------------------------------------------------------------

.PHONY: fmt
fmt: ## Format with black and apply ruff autofixes
	$(RUN) black cascade tests
	$(RUN) ruff check --fix cascade tests

.PHONY: lint
lint: ## ruff + black --check
	$(RUN) ruff check cascade tests
	$(RUN) black --check cascade tests

.PHONY: typecheck
typecheck: ## mypy strict on cascade/
	$(RUN) mypy

.PHONY: test
test: ## Run the test suite (excludes tests needing live services)
	$(RUN) pytest -m "not integration and not network and not leakage"

.PHONY: test-all
test-all: ## Run every test including integration and leakage (requires `make up`)
	$(RUN) pytest -m "not network"

.PHONY: test-leakage
test-leakage: ## The M3 time-lock probes alone (requires `make up` and a built corpus)
	$(RUN) pytest -m leakage

.PHONY: ci
ci: lint typecheck test ## Everything CI runs

# ---------------------------------------------------------------------------
# Infrastructure (M11) -- every gate runs offline, with no AWS account.
# Toolchain versions are pinned and run in Docker, the same versions CI pins,
# so a developer's system Terraform never decides what "valid" means.
# ---------------------------------------------------------------------------

TF_IMAGE ?= hashicorp/terraform:1.16.3
TFLINT_IMAGE ?= ghcr.io/terraform-linters/tflint:v0.64.0
CHECKOV_VERSION ?= 3.3.19
TF_ROOTS := envs/bootstrap envs/sandbox envs/platform
TF_TEST_ROOTS := envs/sandbox envs/platform
TF_LINT_DIRS := envs/sandbox envs/bootstrap envs/platform modules/network modules/database modules/bench modules/governance modules/guardrails modules/eventlake modules/recovery modules/audit modules/cicd modules/observability modules/pipeline modules/egress modules/cache modules/study
DOCKER_TF = docker run --rm -v $(CURDIR):/work -e TF_PLUGIN_CACHE_DIR=/work/.tf-plugin-cache -e TF_IN_AUTOMATION=1

.PHONY: infra-fmt
infra-fmt: ## Format the Terraform
	$(DOCKER_TF) -w /work/infra/terraform $(TF_IMAGE) fmt -recursive

.PHONY: infra-check
infra-check: ## Terraform fmt/validate, offline tests (mock providers), tflint, checkov, Dockerfile lint
	@mkdir -p .tf-plugin-cache/tflint
	$(DOCKER_TF) -w /work/infra/terraform $(TF_IMAGE) fmt -recursive -check
	@for root in $(TF_ROOTS); do \
		$(DOCKER_TF) -w /work/infra/terraform/$$root $(TF_IMAGE) init -backend=false -input=false >/dev/null && \
		$(DOCKER_TF) -w /work/infra/terraform/$$root $(TF_IMAGE) validate || exit 1; \
	done
	@for root in $(TF_TEST_ROOTS); do \
		$(DOCKER_TF) -w /work/infra/terraform/$$root $(TF_IMAGE) test || exit 1; \
	done
	$(DOCKER_TF) -w /work/infra/terraform -e TFLINT_PLUGIN_DIR=/work/.tf-plugin-cache/tflint --entrypoint tflint $(TFLINT_IMAGE) --init --config=/work/infra/terraform/.tflint.hcl
	@for dir in $(TF_LINT_DIRS); do \
		$(DOCKER_TF) -w /work/infra/terraform/$$dir -e TFLINT_PLUGIN_DIR=/work/.tf-plugin-cache/tflint --entrypoint tflint $(TFLINT_IMAGE) --config=/work/infra/terraform/.tflint.hcl || exit 1; \
	done
	uvx --quiet checkov==$(CHECKOV_VERSION) -d infra/terraform --framework terraform --compact --quiet --skip-path .terraform
	docker buildx build --check -f infra/docker/bench.Dockerfile .

# ---------------------------------------------------------------------------
# Study
# ---------------------------------------------------------------------------

.PHONY: doctor
doctor: ## Verify toolchain, pinned stack and services
	$(RUN) cascade doctor

.PHONY: study
study: ## Run the full study end to end (M6+)
	$(RUN) cascade simulate

.PHONY: report
report: ## Write the report artifact (M7+)
	$(RUN) cascade report

.PHONY: demo
demo: ## The 90-second path: four ablation cells -> replay -> trace -> report
	@echo "==> 1/5  building and sealing the scenario registry"
	$(RUN) cascade ledger build
	$(RUN) cascade ledger seal
	@echo "==> 2/5  running the four cells that need no compiled graph"
	$(RUN) cascade eval grid --policy heuristic \
		--cell C09 --cell C10 --cell C11 --cell C12 --limit 40
	@echo "==> 3/5  proving the runs replay byte-identically in fresh processes"
	$(RUN) cascade trace replay --runs 25
	@echo "==> 4/5  walking one outcome back to its root cause"
	$(RUN) cascade trace explain
	@echo "==> 5/5  writing the report artifact"
	$(RUN) cascade report --headline C09

.PHONY: verify
verify: ## Run every structural gate that does not need a credential
	$(RUN) cascade doctor
	$(RUN) cascade ledger verify
	$(RUN) cascade corpus verify
	$(RUN) cascade corpus coverage
	$(RUN) cascade retrieval verify
	$(RUN) cascade trace status
