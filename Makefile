.DEFAULT_GOAL := help
COMPOSE := docker compose
UV := uv run

.PHONY: help install lint format typecheck infra test test-unit test-integration \
        test-adversarial up down clean logs ps train demo generate adversarial-report \
        benchmark docs

help: ## Lista os comandos
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Instala dependências (uv) e os hooks de pre-commit
	uv sync --all-extras
	$(UV) pre-commit install

lint: ## Ruff (lint + formatação)
	$(UV) ruff check .
	$(UV) ruff format --check .

format: ## Formata e corrige o que for automático
	$(UV) ruff format .
	$(UV) ruff check --fix .

typecheck: ## mypy em modo estrito
	$(UV) mypy

infra: ## Sobe só PostgreSQL e Redis (para testes e desenvolvimento local)
	$(COMPOSE) up -d --wait postgres redis

test: infra ## Todos os testes: unitários, integração e suíte adversarial
	$(UV) pytest

test-unit: ## Só os testes unitários (não precisam de Docker)
	$(UV) pytest tests/unit

test-integration: infra ## Testes de integração (API, worker, fila, banco)
	$(UV) pytest tests/integration

test-adversarial: ## Suíte adversarial: os cinco ataques contra o modelo (Fase D)
	$(UV) pytest tests/adversarial -s

up: ## Sobe tudo: banco, fila, migrações, treino (1ª vez), API, worker, painel, Prometheus, Jaeger
	$(COMPOSE) up -d --build --wait
	@echo ""
	@echo "  API         http://localhost:8000/docs"
	@echo "  Painel      http://localhost:8501"
	@echo "  Prometheus  http://localhost:9090"
	@echo "  Jaeger      http://localhost:16686"

down: ## Para tudo (mantém os dados)
	$(COMPOSE) down

clean: ## Para tudo e apaga volumes (banco, fila e modelo)
	$(COMPOSE) down -v

logs: ## Logs da API e do worker
	$(COMPOSE) logs -f --tail=100 api worker

ps: ## Estado dos serviços
	$(COMPOSE) ps -a

train: ## Retreina o modelo dentro do Docker e reinicia quem o usa
	$(COMPOSE) run --rm trainer python -m fraud.ml.train --out /app/artifacts/model
	$(COMPOSE) restart worker api

demo: up ## Demonstração de ponta a ponta (< 5 min)
	$(COMPOSE) run --rm tools python -m fraud.simulation.demo

generate: ## Só o gerador de tráfego sintético contra a API no ar
	$(COMPOSE) run --rm tools python -m fraud.simulation.generator

adversarial-report: ## Reproduz o ciclo adversarial inteiro (~20 min) em docs/adversarial_runs
	$(UV) python -m fraud.ml.adversarial_report

benchmark: ## Benchmark no dataset real da ULB (precisa de data/creditcard.csv)
	$(UV) python -m fraud.ml.benchmark_kaggle

docs: ## Regenera a documentação derivada do código (features e relatório do modelo)
	$(UV) python -m fraud.ml.report --features docs/FEATURES.md
	$(UV) python -m fraud.ml.train --out artifacts/model --report docs/MODEL_REPORT.md
