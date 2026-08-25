# =============================================================================
# lukato 1.0.0
# =============================================================================
SHELL      := /bin/bash
PY         := .venv/bin/python
PIP        := .venv/bin/pip
UVICORN    := .venv/bin/uvicorn
RUFF       := .venv/bin/ruff
MYPY       := .venv/bin/mypy
PYTEST     := .venv/bin/pytest
ALEMBIC    := .venv/bin/alembic
IMAGE      := lukato:1.0.0
export PYTHONPATH := src

.DEFAULT_GOAL := help

.PHONY: help
help: ## mostra esta ajuda
	@grep -hE '^[a-zA-Z0-9_.-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- ambiente ----------------------------------------------------------------
.PHONY: venv install install-dev install-media env
venv: ## cria a virtualenv
	python3 -m venv .venv && $(PIP) install --upgrade pip setuptools wheel

install: venv ## instala dependencias de runtime
	$(PIP) install -r requirements.txt

install-dev: venv ## instala runtime + ferramentas de qualidade
	$(PIP) install -r requirements-dev.txt

install-media: ## instala o pipeline multimodal opcional (pesado)
	$(PIP) install -r requirements-media.txt

env: ## cria .env a partir do modelo
	@test -f .env || (cp .env.example .env && echo "criado .env — preencha os segredos")

# --- execucao ----------------------------------------------------------------
.PHONY: run dev seed shell
run: ## sobe a API + console
	$(UVICORN) lukato.main:app --host 0.0.0.0 --port 8000

dev: ## sobe com reload automatico
	$(UVICORN) lukato.main:app --reload --host 127.0.0.1 --port 8000

seed: ## popula prompts, guardrails, modulos e catalogo de demonstracao
	$(PY) -m lukato.interfaces.cli seed

shell: ## shell python com o pacote no path
	$(PY)

# --- qualidade ---------------------------------------------------------------
.PHONY: lint fmt type test test-unit test-int cov check
lint: ## ruff check
	$(RUFF) check src tests

fmt: ## ruff format + fix
	$(RUFF) check src tests --fix && $(RUFF) format src tests

type: ## mypy
	$(MYPY) src/lukato

test: ## suite completa
	$(PYTEST)

test-unit: ## somente testes de unidade
	$(PYTEST) -m unit

test-int: ## somente testes de integracao
	$(PYTEST) -m integration

cov: ## cobertura
	$(PYTEST) --cov=src/lukato --cov-report=term-missing --cov-report=html

check: lint type test ## portao de qualidade completo

# --- banco -------------------------------------------------------------------
.PHONY: migrate migration downgrade
migrate: ## aplica migracoes
	$(ALEMBIC) upgrade head

migration: ## cria migracao autogerada (m="mensagem")
	$(ALEMBIC) revision --autogenerate -m "$(m)"

downgrade: ## volta uma migracao
	$(ALEMBIC) downgrade -1

# --- contratos ---------------------------------------------------------------
.PHONY: openapi
openapi: ## exporta o contrato OpenAPI para specs/contracts/
	$(PY) -m lukato.interfaces.cli openapi --out specs/contracts/openapi.json

# --- containers --------------------------------------------------------------
.PHONY: docker-build docker-run up down logs ps
docker-build: ## constroi a imagem
	docker build -t $(IMAGE) .

docker-build-mirror: ## constroi usando um registry espelho (rede restrita); PYTHON_IMAGE=<ref>
	@test -n "$(PYTHON_IMAGE)" || (echo "informe PYTHON_IMAGE=<registry>/python:3.11-slim-bookworm"; exit 1)
	docker build --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE) -t $(IMAGE) .

docker-run: ## roda a imagem isolada
	docker run --rm -p 8000:8000 --env-file .env $(IMAGE)

up: ## sobe a stack local (postgres + api)
	docker compose up -d --build

down: ## derruba a stack local
	docker compose down -v

logs: ## acompanha os logs da api
	docker compose logs -f api

ps: ## estado da stack
	docker compose ps

# --- kubernetes --------------------------------------------------------------
.PHONY: k8s-render k8s-apply k8s-delete
k8s-render: ## renderiza os manifestos
	kubectl kustomize deploy/k8s/overlays/dev

k8s-apply: ## aplica no cluster corrente
	kubectl apply -k deploy/k8s/overlays/dev

k8s-delete: ## remove do cluster corrente
	kubectl delete -k deploy/k8s/overlays/dev

# --- limpeza -----------------------------------------------------------------
.PHONY: clean
clean: ## remove artefatos locais
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml dist build
