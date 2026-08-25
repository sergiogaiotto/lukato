# lukato · 1.0.0

**Ecossistema de agentes de IA modulares e escalaveis.**
Cada funcionalidade e um *building block* independente. Para **todo e qualquer modulo**,
a trinca **guardrail de entrada → system prompt → guardrail de saida** e parametrizavel
— sem escrever codigo.

```
                  ┌──────────────────────────────────────────────────────────┐
                  │                   NUCLEO DA PLATAFORMA                   │
                  │ registry · composer · guardrails · runs · finops · trace │
                  └───┬──────────┬───────────┬───────────┬───────────┬───────┘
                      │          │           │           │           │
                 ┌────▼───┐ ┌────▼─────┐ ┌───▼────┐ ┌────▼─────┐ ┌───▼─────┐
                 │  auth  │ │processing│ │ finops │ │knowledge │ │ adwatch │
                 └────────┘ └──────────┘ └────────┘ └──────────┘ └─────────┘
                        building blocks — plugaveis, versionados, isolados
```

| | |
| --- | --- |
| **Abordagem** | Spec-Driven Development — `specs/` e a fonte da verdade |
| **Arquitetura** | Hexagonal (Ports & Adapters) |
| **Linguagem** | Python 3.11 |
| **API** | FastAPI · OpenAPI 3.1 · Swagger UI |
| **Agentes** | LangGraph · Deep-Agent Harness (`deepagents`) |
| **LLM** | Qwen (`qwen-latest`) e `openai/gpt-oss-20b` via hub GPU corporativo (API compativel com OpenAI) |
| **Embeddings** | `Qwen/Qwen3-Embedding-0.6B` — 1024 dimensoes, colecao pgvector `agente_evidence` |
| **Banco** | PostgreSQL 16 + pgvector (fallback SQLite em dev/testes) |
| **UI** | Jinja2 (template engine), tres colunas, menu recolhivel, painel de contexto |
| **Observabilidade** | Langfuse · structlog · Prometheus |
| **Implantacao** | Docker multi-stage non-root · Kustomize para Kubernetes |

---

## Inicio rapido

### 1. Local, sem nada instalado alem do Python

```bash
make install-dev          # cria .venv e instala tudo
make env                  # cria .env a partir de .env.example
make seed                 # prompts, guardrails, modulos e catalogo de demonstracao
make run                  # http://localhost:8000
```

Sem chave de LLM e sem PostgreSQL a aplicacao **sobe do mesmo jeito**: o adaptador de
LLM cai para o modo `echo` determinista, os embeddings para o modo `hashing`, o tracer
para `noop` e o banco para SQLite. `/readyz` informa exatamente o que esta degradado.
E o modo pensado para desenvolver e testar sem depender da rede corporativa.

### 2. Stack completa com Docker

```bash
make up                   # postgres+pgvector e a aplicacao
make logs
make down
```

### 3. Kubernetes

```bash
make k8s-render           # revisa os manifestos renderizados
make k8s-apply            # aplica o overlay de dev
```

| Endereco | O que e |
| --- | --- |
| `http://localhost:8000/` | console web |
| `http://localhost:8000/api/docs` | Swagger UI |
| `http://localhost:8000/api/redoc` | ReDoc |
| `http://localhost:8000/api/openapi.json` | contrato OpenAPI 3.1 |
| `http://localhost:8000/healthz` · `/readyz` | probes |
| `http://localhost:8000/metrics` | Prometheus |

---

## A ideia central: building blocks com guardrails parametrizaveis

Um modulo nao carrega regra de negocio propria. Ele carrega um **binding**:

```jsonc
{
  "slug": "triagem-atendimento",
  "runtime": "langgraph",
  "binding": {
    "input_guardrail_id":  "<politica de entrada>",   // bloqueia PII, injecao, segredos
    "system_prompt_id":    "<system prompt versionado>",
    "output_guardrail_id": "<politica de saida>",     // valida schema, redige PII
    "model": "qwen-latest",
    "temperature": 0.2,
    "tools": ["knowledge_search"]
  }
}
```

Criar um agente novo e criar uma `ModuleDefinition` — **nao** e escrever codigo.
Trocar a politica de guardrail de um modulo em producao e um `PUT`, sem redeploy.

O fluxo de execucao e sempre o mesmo, e nenhuma etapa pode ser pulada:

```
entrada → [guardrail de entrada] → [system prompt] → [runtime] → [guardrail de saida] → resposta
                    ↓ bloqueio                                          ↓ bloqueio
              run BLOCKED, 422                                    run BLOCKED, 422
              (o provedor nunca e chamado)
```

Toda invocacao vira um `AgentRun` persistido, com steps, tokens, custo e `trace_id`.

---

## Modulos embutidos

| slug | tipo | o que faz |
| --- | --- | --- |
| `auth` | auth | login, JWT, chaves de API, RBAC (`root`/`admin`/`operator`/`viewer`) |
| `processing` | agent | agente generico — todo o comportamento vem do binding |
| `finops` | finops | custo por modulo/modelo/tenant, orcamentos, alertas e bloqueio |
| `knowledge` | knowledge | ingestao, chunking, embeddings Qwen, busca semantica com pgvector |
| `adwatch` | pipeline | catalogo de comerciais (CRUD) + deteccao temporal multimodal em video |

Modulos de terceiros entram pelo entry point `lukato.modules` — o nucleo nao muda.

---

## AdWatch: encontrar comerciais em horas de video

Como **o texto dos comerciais ja e conhecido**, o problema nao e classificacao aberta de
video: e **matching temporal multimodal**. O modelo multimodal entra no fim do funil,
como juiz, e nao no inicio.

```
VIDEO ──┬── audio ──→ ASR (WhisperX) ──→ palavras + timestamps ──┐
        └── frames ─→ scene detect + OCR ─→ texto na tela ───────┤
                                                                 ▼
                                              janelas 15/30/60 s
                                                                 ▼
                                        retrieval sobre fingerprints → TOP-K
                                                                 ▼
                                    S = 0.40·lexico + 0.25·semantico
                                      + 0.15·ocr + 0.15·visual + 0.05·duracao
                                                                 ▼
                            S ≥ 0.90 aceita │ 0.60–0.90 juiz Qwen │ < 0.60 rejeita
                                                                 ▼
                                        refino de fronteira por cortes de cena
```

O caminho de **importacao de transcricao** (JSON no formato WhisperX) torna o pipeline
inteiro executavel sem FFmpeg, sem GPU e sem rede — e o caminho usado nos testes.

Detalhes normativos: [`specs/0010-adwatch.spec.md`](specs/0010-adwatch.spec.md).

---

## Estrutura

```
specs/          especificacoes normativas (SDD) — o codigo obedece a elas
docs/           arquitetura, ADRs, notas de biblioteca
src/lukato/
  domain/       nucleo puro: modelos, portas, servicos (zero I/O)
  application/  casos de uso
  adapters/     driven: persistencia, LLM, embeddings, guardrails, runtime, tracing, midia
  interfaces/   driving: HTTP (API v1), UI (Jinja2), CLI
  modules/      building blocks + registry
migrations/     Alembic
deploy/k8s/     Kustomize (base + overlays dev/prod)
tests/          unit · integration · contract
```

A regra de dependencia e verificada por teste automatizado: `domain/` nao importa
`sqlalchemy`, `fastapi`, `httpx`, `openai`, `langgraph`, `langfuse` nem `jinja2`.

---

## Configuracao

Tudo por variavel de ambiente, prefixo `LUKATO_`, aninhamento com `__`
(`LUKATO_LLM__MODEL` → `settings.llm.model`). Veja
[`.env.example`](.env.example) para a lista completa e comentada.

> **Segredos.** A chave do hub GPU, o segredo JWT e as credenciais Langfuse vao para
> cofre corporativo (Vault, AWS Secrets Manager, ExternalSecrets) — nunca para o
> repositorio nem para a imagem. O `.env` esta no `.gitignore`; em Kubernetes os
> segredos chegam por `secretKeyRef`.

---

## Qualidade

```bash
make lint     # ruff
make type     # mypy (estrito em domain/ e application/)
make test     # pytest — roda offline, sem PostgreSQL e sem rede
make check    # os tres
```

---

## Documentacao

| Documento | Conteudo |
| --- | --- |
| [`specs/0000-core-contracts.spec.md`](specs/0000-core-contracts.spec.md) | contratos nucleares (normativo) |
| [`specs/0001`](specs/0001-plataforma-building-blocks.spec.md) · [`0002`](specs/0002-registry-modulos.spec.md) | plataforma e registry |
| [`specs/0003`](specs/0003-guardrails.spec.md) · [`0004`](specs/0004-runtime-agentes.spec.md) | guardrails e runtimes de agente |
| [`specs/0005`](specs/0005-finops.spec.md) · [`0006`](specs/0006-identidade-acesso.spec.md) | FinOps e identidade |
| [`specs/0007`](specs/0007-conhecimento-embeddings.spec.md) · [`0008`](specs/0008-observabilidade.spec.md) | conhecimento e observabilidade |
| [`specs/0009`](specs/0009-console-ui.spec.md) · [`0010`](specs/0010-adwatch.spec.md) | console web e AdWatch |
| [`specs/0011`](specs/0011-persistencia.spec.md) · [`0012`](specs/0012-deploy-kubernetes.spec.md) | persistencia e Kubernetes |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | visao de arquitetura |
| [`docs/LIBRARY-NOTES.md`](docs/LIBRARY-NOTES.md) | APIs reais das versoes usadas |
| [`readme.txt`](readme.txt) | guia operacional em texto puro |
