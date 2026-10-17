SHELL := /bin/bash
PY ?= .venv/bin/python
PROMETHEUS_IMAGE := prom/prometheus:v3.5.0
ALERTMANAGER_IMAGE := prom/alertmanager:v0.28.1
RELEASE ?=

.DEFAULT_GOAL := help

help: ## List targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- development ---------------------------------------------------------

install: ## Create .venv and install the package with dev tools (needs uv)
	uv venv -q .venv -p 3.12
	uv pip install -q -p .venv -e ".[dev]"

lint: ## Ruff lint + format check
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

test: ## Unit tests
	$(PY) -m pytest -q

test-rules: ## promtool + amtool: validate and unit-test alert rules and routing
	docker run --rm -v $(PWD)/deploy/prometheus:/p -w /p --entrypoint promtool $(PROMETHEUS_IMAGE) check rules rules/recording.yml rules/alerts.yml
	docker run --rm -v $(PWD)/deploy/prometheus:/p -w /p/tests --entrypoint promtool $(PROMETHEUS_IMAGE) test rules alerts.test.yml
	docker run --rm -v $(PWD)/deploy/alertmanager:/c --entrypoint amtool $(ALERTMANAGER_IMAGE) check-config /c/alertmanager.yml
	@# routing contract: canary alerts never page (the controller rolls back), stable criticals do
	test "$$(docker run --rm -v $(PWD)/deploy/alertmanager:/c --entrypoint amtool $(ALERTMANAGER_IMAGE) config routes test --config.file=/c/alertmanager.yml track=canary severity=critical)" = chat
	test "$$(docker run --rm -v $(PWD)/deploy/alertmanager:/c --entrypoint amtool $(ALERTMANAGER_IMAGE) config routes test --config.file=/c/alertmanager.yml track=stable severity=critical)" = page

generated: ## Regenerate Grafana dashboards and the k8s PrometheusRule
	$(PY) scripts/build_dashboards.py
	$(PY) scripts/gen_k8s_rules.py

check: lint test test-rules ## Everything CI runs before building

lock: ## Re-pin requirements.lock from pyproject.toml
	uv pip compile -q pyproject.toml -o requirements.lock --python-version 3.12

# --- local stack ---------------------------------------------------------

state: ## Seed deploy/state (stable = v1.4.0) if missing
	$(PY) -m canary.controller init --release v1.4.0

build: ## Build the agent image
	docker compose build agent-stable

up: state ## Start the whole stack (agent x2, traefik, collector, phoenix, prometheus, alertmanager, grafana)
	docker compose up -d
	@echo "API         http://localhost:8080/v1/chat"
	@echo "Grafana     http://localhost:3000   (dashboards: Agent overview, Agent canary analysis)"
	@echo "Phoenix     http://localhost:6006   (traces)"
	@echo "Prometheus  http://localhost:9090   Alertmanager http://localhost:9093   Alerts sent: http://localhost:9095"

down: ## Stop the stack (keeps volumes and deploy/state)
	docker compose down

reset: ## Stop the stack and delete volumes and release state
	docker compose down -v
	rm -rf deploy/state

ps: ## Container status
	docker compose ps

logs: ## Follow agent logs
	docker compose logs -f agent-stable agent-canary

ask: ## Send one question: make ask Q="Where is my order 10232?"
	@curl -s localhost:8080/v1/chat -H 'content-type: application/json' -d "$$(jq -n --arg m "$(Q)" '{message: $$m}')" | jq .

evals: ## Golden evals through the gateway
	$(PY) -m evals.run_evals --url http://localhost:8080

load: ## Open-loop traffic: make load RPS=3 DURATION=600
	$(PY) -m load.loadgen --rps $(or $(RPS),2) --duration $(or $(DURATION),300)

# --- releases ------------------------------------------------------------

canary: ## Ship a release through eval gate + canary analysis: make canary RELEASE=v1.4.1
	@test -n "$(RELEASE)" || (echo "usage: make canary RELEASE=v1.4.1" && exit 2)
	$(PY) -m canary.controller deploy $(RELEASE)

rollback: ## Abort the running canary, or revert stable to the previous release
	$(PY) -m canary.controller rollback

status: ## Current stable/canary release and traffic split
	$(PY) -m canary.controller status

drill: ## Scripted drill: promote a good release, auto-roll back a bad one, manual rollback (~15 min)
	./scripts/drill.sh

.PHONY: help install lint test test-rules generated check lock state build up down reset ps logs ask evals load canary rollback status drill
