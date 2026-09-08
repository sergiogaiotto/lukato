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
| **Linguagem** | Python 3.11 (3.12 suportado) |
| **API** | FastAPI · OpenAPI 3.1 · 66 rotas · 91 operacoes · Swagger UI |
| **Agentes** | LangGraph · Deep-Agent Harness (`deepagents`) · runtime direto |
| **LLM** | Qwen (`qwen-latest`) e `openai/gpt-oss-20b` via hub GPU corporativo (API compativel com OpenAI) |
| **Embeddings** | `Qwen/Qwen3-Embedding-0.6B` — 1024 dimensoes, colecao pgvector `agente_evidence` |
| **Banco** | PostgreSQL 16 + pgvector (fallback SQLite em dev/testes) · 18 tabelas · 2 migracoes |
| **UI** | Jinja2, tres colunas, menu recolhivel, painel de contexto, paleta de comandos |
| **Observabilidade** | Langfuse · structlog · Prometheus (9 metricas) |
| **Implantacao** | Docker multi-stage non-root · Kustomize para Kubernetes |

---

## Sumario

1. [O que e o lukato](#1-o-que-e-o-lukato)
2. [A invariante central: a trinca](#2-a-invariante-central-a-trinca)
3. [Arquitetura](#3-arquitetura)
4. [Inicio rapido](#4-inicio-rapido)
5. [**Uso da aplicacao**](#5-uso-da-aplicacao) — console, API, CLI e receitas ponta a ponta
6. [Building blocks: criar, versionar, distribuir](#6-building-blocks-criar-versionar-distribuir)
7. [Guardrails](#7-guardrails)
8. [Runtimes de agente e ferramentas](#8-runtimes-de-agente-e-ferramentas)
9. [Conhecimento e embeddings](#9-conhecimento-e-embeddings)
10. [AdWatch: deteccao temporal multimodal](#10-adwatch-deteccao-temporal-multimodal)
11. [FinOps](#11-finops)
12. [Identidade e acesso](#12-identidade-e-acesso)
13. [Observabilidade](#13-observabilidade)
14. [Modo degradado](#14-modo-degradado)
15. [Configuracao](#15-configuracao)
16. [Persistencia e migracoes](#16-persistencia-e-migracoes)
17. [Implantacao](#17-implantacao)
18. [Qualidade e provas executaveis](#18-qualidade-e-provas-executaveis)
19. [Estrutura de diretorios](#19-estrutura-de-diretorios)
20. [Documentacao normativa](#20-documentacao-normativa)
21. [Solucao de problemas](#21-solucao-de-problemas)
22. [Seguranca](#22-seguranca)

---

## 1. O que e o lukato

### 1.1 O problema

Quem coloca agentes de IA em producao dentro de uma empresa esbarra sempre nos mesmos
quatro problemas, e nenhum deles e "qual modelo usar":

| Problema | Como costuma ser resolvido | Por que quebra |
| --- | --- | --- |
| Cada agente novo e um projeto novo | um repositorio, um deploy, um time por agente | o custo marginal do decimo agente e igual ao do primeiro |
| A politica de conteudo vive no codigo | `if "cpf" in texto: ...` espalhado | trocar uma regra exige *pull request*, revisao e redeploy |
| A garantia de seguranca e convencao | "combinamos que todo agente chama o filtro" | convencao quebra em silencio, e ninguem percebe ate vazar |
| O custo aparece na fatura | rateio mensal por estimativa | descobre-se o estouro trinta dias depois |

O lukato ataca os quatro de uma vez, com uma unica decisao estrutural: **a
funcionalidade de um agente e configuracao, nao codigo**, e a plataforma e a unica
dona do caminho de execucao.

### 1.2 A tese

Um agente, no lukato, e uma linha no banco: uma `ModuleDefinition`. Ela nao contem
logica — contem um **binding**, que amarra tres objetos versionados (uma politica de
guardrail de entrada, um system prompt, uma politica de guardrail de saida) a uma
classe de building block ja instalada, mais o modelo, a temperatura e as ferramentas.

Consequencias diretas, todas verificaveis neste repositorio:

- criar um agente e um `POST /api/v1/modules` — sem repositorio novo, sem deploy;
- trocar a politica de seguranca de um agente **em producao** e um `PUT`, sem redeploy;
- duas definicoes sobre a **mesma classe** produzem comportamentos diferentes (o seed
  entrega `assistente` e `triagem` exatamente assim, e `scripts/prova_trinca.py` afirma
  isso em uma asercao executavel);
- nenhum modulo consegue chamar o provedor de LLM por fora da trinca, porque nenhum
  modulo recebe um cliente de LLM: recebe uma porta, injetada pelo caso de uso que ja
  executou o guardrail de entrada.

### 1.3 O que o lukato nao e

- **Nao e um framework de agentes.** LangGraph e `deepagents` sao adaptadores por tras
  de uma porta (`OrchestratorPort`); podem ser trocados sem tocar em regra de negocio.
- **Nao e um wrapper de API de LLM.** O provedor e um detalhe de borda com irmao
  deterministico offline.
- **Nao e um classificador de video.** O AdWatch parte do principio oposto (secao 10):
  o texto procurado ja e conhecido, entao o problema e alinhamento temporal, e o modelo
  multimodal so entra como juiz no fim do funil.

### 1.4 Vocabulario

Sete termos aparecem o tempo todo neste documento. Vale fixa-los antes.

| Termo | O que e |
| --- | --- |
| **trinca** | guardrail de entrada → system prompt → guardrail de saida. A invariante central. |
| **building block** | a **classe** de um modulo — codigo instalado, descoberto por entry point |
| **definicao** (`ModuleDefinition`) | a **configuracao** de um agente — uma linha no banco, apontando para uma classe |
| **binding** | o campo da definicao que amarra a trinca, o modelo, a temperatura e as ferramentas |
| **run** (`AgentRun`) | o registro persistido de uma invocacao, com passos, tokens, custo e trace |
| **fingerprint** | a assinatura de um comercial: texto normalizado, tokens, ancoras e vetor semantico |
| **janela** | um recorte temporal da transcricao (15, 30 ou 60 s) confrontado com os fingerprints |

A distincao que mais importa e a primeira: **classe e codigo, definicao e configuracao**.
Duas definicoes sobre a mesma classe sao dois agentes diferentes, e nenhuma linha de
codigo os separa.

---

## 2. A invariante central: a trinca

### 2.1 O binding

```jsonc
{
  "slug": "triagem-atendimento",
  "name": "Triagem de atendimento",
  "kind": "agent",
  "runtime": "langgraph",
  "binding": {
    "input_guardrail_id":  "<politica de entrada>",   // bloqueia PII, injecao, segredos
    "system_prompt_id":    "<system prompt versionado>",
    "output_guardrail_id": "<politica de saida>",     // valida schema, redige PII
    "model": "qwen-latest",
    "temperature": 0.2,
    "max_tokens": 512,
    "timeout_seconds": 30.0,
    "tools": ["knowledge_search"]
  },
  "config": { "module": "processing", "max_iterations": 1, "planning": false },
  "status": "active"
}
```

`config.module` (ou `config.implementation`) aponta a **classe** do building block;
`slug` identifica a **definicao**. E essa separacao que permite `triagem` e `juridico`
serem a mesma classe `processing` com comportamentos distintos — nenhuma linha de codigo
separa os dois.

### 2.2 O caminho unico de execucao

`InvokeModule` e o **unico** lugar onde um building block executa. Ele cumpre onze
etapas normativas (SPEC-0001 secao 4) nesta ordem exata, sem atalho:

```
 1  resolve a definicao            7  renderiza o system prompt
 2  exige status ACTIVE            8  executa o modulo (module.handle)
 3  exige permissao MODULE_INVOKE  9  GUARDRAIL DE SAIDA
 4  verifica orcamentos FinOps    10  grava UsageRecord + custo
 5  abre trace + AgentRun         11  finaliza o run e commita
 6  GUARDRAIL DE ENTRADA
```

A trinca **envolve** o `handle`, seja qual for o building block:

```
entrada → [guardrail de entrada] → [system prompt] → [runtime] → [guardrail de saida] → resposta
                    ↓ bloqueio                                          ↓ bloqueio
              run BLOCKED, 422                                    run BLOCKED, 422
              (o provedor nunca e chamado)
```

Duas garantias estruturais, e nao documentais:

- **nada e enviado ao provedor antes da etapa 6.** Um bloqueio de entrada encerra a
  execucao com `AgentRun(BLOCKED)` e HTTP 422, e o texto barrado nunca sai da plataforma;
- **nao existe execucao invisivel.** Qualquer excecao entre as etapas 5 e 11 grava
  `AgentRun(FAILED)` antes de propagar.

### 2.3 A prova executavel

Ler o codigo nao e a unica forma de conferir. `scripts/prova_trinca.py` monta um banco
descartavel, substitui o LLM por um `EchoLLM` **instrumentado que conta chamadas** e roda
sete asercoes. Saida real:

```
=== 3. prompt injection (deve BLOQUEAR antes do provedor) ===
  bloqueado: code=guardrail_violation http=422 stage=input
  CHAMADAS AO PROVEDOR APOS O BLOQUEIO: 0  <- tem que ser 0

=== 4. duas definicoes, MESMA classe, comportamentos diferentes ===
  triagem  system prompt: ...Voce e um atendente da Claro. Responda com objetividade.
  juridico system prompt: ...Voce e o juridico da Claro. Cite sempre a clausula aplic
  mesma classe 'processing', prompts diferentes: True

=== 7. o run foi persistido em todos os casos? ===
  triagem   succeeded custo=9.6e-05   steps=['guardrail_in', 'prompt', 'llm', 'guardrail_out']
  triagem   blocked   custo=0.0       steps=['guardrail_in']
  juridico  succeeded custo=6.8e-05   steps=['guardrail_in', 'prompt', 'llm', 'guardrail_out']
```

O contador em zero transforma "o guardrail bloqueia antes do provedor" de afirmacao em
evidencia. E a trilha de passos mostra que o run bloqueado parou em `guardrail_in`: nao
houve `prompt`, nao houve `llm`.

### 2.4 O quadro de invariantes

O que distingue este projeto nao e ter as garantias abaixo escritas em algum lugar — e
elas serem **mecanicamente impostas** e **verificadas por teste**. Nenhuma depende de
alguem lembrar.

| # | Invariante | Onde e imposta | Como e verificada |
| --- | --- | --- | --- |
| 1 | Todo building block executa pela trinca; nao ha outro caminho | `InvokeModule` e o unico ponto de execucao | `prova_trinca.py` 1–2 · `test_module_lifecycle.py` |
| 2 | Bloqueio de entrada precede qualquer chamada ao provedor | etapa 6 antes da etapa 8 | `prova_trinca.py` 3 (contador de chamadas em zero) |
| 3 | Nenhum modulo instancia cliente de LLM | recebe `LLMPort` pelo `ModuleContext` | `test_architecture.py` |
| 4 | Toda execucao vira `AgentRun` persistido — sucesso, bloqueio ou falha | etapas 5 e 11, com `FAILED` gravado antes de propagar | `prova_trinca.py` 7 |
| 5 | Somente definicao `active` e invocavel | etapa 2 | `prova_trinca.py` 5 (`409`) |
| 6 | Invocar exige `MODULE_INVOKE` | etapa 3 | `prova_trinca.py` 6 (`403`) |
| 7 | Orcamento com `hard_stop` recusa antes de gastar | etapa 4 | `test_api_knowledge_finops.py` |
| 8 | `domain/` nao importa framework nem adaptador | `ruff` banned-api + varredura por diretorio | `test_architecture.py` |
| 9 | Os pesos de fusao do AdWatch somam 1.0 | `ScoreFusion.__init__` recusa (tolerancia 1e-6) | `test_matching.py` · `test_settings.py` |
| 10 | `review_threshold` ≤ `accept_threshold` | `ScoreFusion.classify` recusa | `test_adwatch_scoring.py` · `test_settings.py` |
| 11 | Embeddings nao degradam sozinhos; a colecao recusa provider/dimensao divergente | `EmbeddingSettings` + metadados da colecao | `test_embeddings.py` · `test_vector_store.py` |
| 12 | Sem OCR, o teto de score fica **abaixo** do limiar de aceite | aritmetica dos pesos, exposta em `/capabilities` | `test_adwatch_scoring.py` · `prova_adwatch.py` 5 |
| 13 | Sem juiz multimodal, `visual_match` **herda** a fala e viaja marcado — nunca 1.0 inventado | `CandidateBuilder.evaluate` | `test_adwatch_scoring.py` · `test_matching.py` |
| 14 | O schema testado em CI e o mesmo implantado | trilha unica de migracao com `render_item` | `test_migrations.py` (tabela a tabela) |
| 15 | Nenhum segredo real versionado | `.gitignore` + placeholders nos manifestos | job de CI dedicado |
| 16 | Um formulario forjado nao escolhe qualquer verbo | `_method` aceita so `PUT`/`PATCH`/`DELETE` | `test_console_forms.py` |

---

## 3. Arquitetura

### 3.1 Hexagonal, com a regra de dependencia verificada por teste

```text
                       ┌───────────────────────────────────┐
   driving adapters    │            APPLICATION            │    driven adapters
   (quem chama)        │        (casos de uso)             │    (quem e chamado)
                       │   ┌───────────────────────────┐   │
  HTTP  ──────────────▶│   │          DOMAIN           │   │◀────────── PostgreSQL
  UI (Jinja2) ────────▶│   │  modelos · portas ·       │   │◀────────── pgvector
  CLI  ───────────────▶│   │  servicos puros           │   │◀────────── Qwen (LLM)
  modules ────────────▶│   └───────────────────────────┘   │◀────────── Qwen (embeddings)
                       └───────────────────────────────────┘◀────────── Langfuse
                                                             ◀────────── FFmpeg/WhisperX/OCR
```

As setas apontam sempre para dentro. `domain/` nao conhece `sqlalchemy`, `fastapi`,
`httpx`, `openai`, `langgraph`, `langfuse` nem `jinja2` — e isso e **verificado**, em
duas camadas:

- estaticamente, por `ruff` (`flake8-tidy-imports.banned-api` proibe `lukato.adapters`
  em `domain/` e `application/`);
- por teste, em `tests/unit/test_architecture.py`, que varre diretorio por diretorio e
  inclui imports feitos dentro de funcoes.

O unico lugar que enxerga tudo ao mesmo tempo e o *composition root*
(`src/lukato/composition.py`).

### 3.2 As tres camadas de execucao

| Camada | Pergunta que responde | Exemplo |
| --- | --- | --- |
| `domain` | *quais sao as regras?* | "um bloqueio de guardrail interrompe a cadeia" |
| `application` | *qual e o passo a passo?* | `InvokeModule`: guardrail → prompt → runtime → guardrail → custo → run |
| `adapters` | *como falo com o mundo?* | `OpenAICompatibleLLM`, `SqlAlchemyRunRepository`, `LangfuseTracer` |

O ganho concreto: o motor de guardrails, o calculo de custo e o motor de matching do
AdWatch sao **funcoes puras sobre modelos puros** — testaveis sem banco, sem rede e sem
mock de biblioteca.

### 3.3 Portas e adaptadores

| Porta (`domain/ports/`) | Adaptadores (`adapters/`) |
| --- | --- |
| `LLMPort` | `OpenAICompatibleLLM` · `EchoLLM` (offline determinista) |
| `EmbeddingsPort` | `QwenEmbedder` · `HashingEmbedder` (offline, sob pedido explicito) |
| `VectorStorePort` | `PgVectorStore` (HNSW, `vector_cosine_ops`) · cosseno em memoria no SQLite |
| `OrchestratorPort` | `direct` · `langgraph` · `deepagent` |
| `GuardrailPort` | `keywords` · `pii` · `secrets_scan` · `schema_json` · `topic` (+ regras puras) |
| `ObservabilityPort` | `LangfuseTracer` · `NoopTracer` |
| `MediaPort` (probe/asr/ocr/scenes/vision) | FFmpeg · WhisperX · PaddleOCR · PySceneDetect · Qwen-VL · importadores JSON |
| `UnitOfWork` / `repositories` | SQLAlchemy 2 async (12 repositorios) |

Trocar qualquer um deles e escrever um adaptador. Nenhuma linha de `domain/` muda.

### 3.4 O composition root

`src/lukato/composition.py` e o **unico** modulo autorizado a importar `adapters`,
`application` e `interfaces` ao mesmo tempo. Todo o resto do sistema so conhece portas;
quem decide qual implementacao ocupa cada porta e uma funcao, uma vez por processo.
`build_container` resolve banco, LLM, embeddings, guardrails, tracer, orquestradores,
registry, precos, seguranca e capacidades multimodais; `dispose_container` desfaz na
ordem inversa, sem deixar telemetria nem pool de conexao pendurados.

Duas regras dessa montagem valem citar, porque explicam o comportamento de boot:

**O log de selecao e parte do contrato.** Cada porta emite uma linha INFO
`port_adapter_selected` com o adaptador escolhido, se ele esta degradado e **o motivo** da
escolha — chave ausente, biblioteca ausente, ping que falhou. Sem isso, uma instalacao
rodando com `EchoLLM` e `HashingEmbedder` responderia `200` em tudo e pareceria saudavel.
O modo degradado tem de ser legivel por quem opera, nao so por quem le o codigo.

**A montagem nunca falha por indisponibilidade de rede.** O que derruba o boot e defeito
de configuracao ou de esquema — coisas que nao se resolvem sozinhas em producao. E ha uma
distincao fina entre os dois caminhos que levam ao modo degradado:

- **configuracao escolhe o adaptador** — sem `LUKATO_LLM__API_KEY` entra o `EchoLLM`, sem
  endpoint de embeddings entra o `HashingEmbedder`, sem chaves do Langfuse entra o
  `NoopTracer`. Decisao estavel e previsivel;
- **sonda apenas classifica** — um hub que nao responde marca a porta como `degraded` no
  log e em `/readyz`, mas **nao troca** o adaptador. As duas excecoes sao deliberadas: o
  banco troca por SQLite quando o `ping` falha (e o que `LUKATO_DB__AUTO_FALLBACK`
  autoriza explicitamente) e o Langfuse vira `NoopTracer` quando o `auth_check()` falha.

---

## 4. Inicio rapido

### 4.1 Do zero ao ar, em um comando

```bash
git clone <repositorio> && cd lukato
make start                # instala, cria .env, semeia e sobe
```

`make start` imprime a URL ao final — `http://localhost:8000` com o `.env` padrao, ou a
porta que estiver em `LUKATO_APP__PORT`.

Passo a passo, se preferir enxergar as partes:

```bash
make install-dev          # cria .venv e instala runtime + ferramentas
make env                  # cria .env a partir de .env.example
make seed                 # prompts, guardrails, modulos e catalogo de demonstracao
make run                  # sobe no host/porta do .env
```

Sem Make:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
PYTHONPATH=src .venv/bin/python -m lukato.interfaces.cli seed
PYTHONPATH=src .venv/bin/python -m lukato.interfaces.cli serve
```

### 4.2 Modo offline completo (sem rede, sem chave, sem PostgreSQL)

A aplicacao **sobe e e util** sem chave de LLM, sem PostgreSQL, sem GPU e sem rede. Para
o modo offline integral — inclusive embeddings — peca os dois adaptadores deterministas
explicitamente:

```bash
export LUKATO_LLM__PROVIDER=echo          # EchoLLM determinista
export LUKATO_EMBEDDING__PROVIDER=hashing # HashingEmbedder local
make seed
make run
```

`GET /readyz` informa exatamente o que esta degradado. Este e o modo usado pela suite de
testes e pelos scripts de prova.

> **Atencao ao `.env` gerado.** Em `.env.example`, uma linha com valor vazio seguida de
> comentario na mesma linha (`LUKATO_LLM__API_KEY=   # <<< SEGREDO`) faz o comentario ser
> lido como o valor da variavel. O efeito e que a aplicacao **acredita ter credencial** e
> tenta falar com o hub corporativo em vez de cair para o eco. Se `lukato modules invoke`
> devolver `provider_error`, apague o comentario da linha da chave ou fixe
> `LUKATO_LLM__PROVIDER=echo`.

### 4.3 Stack completa com Docker

```bash
make up                   # postgres+pgvector e a aplicacao
make logs                 # acompanha
make ps                   # estado
make down                 # derruba e limpa volumes
```

Com o perfil de observabilidade (Langfuse local em `http://localhost:3000`):

```bash
docker compose --profile obs up -d
```

### 4.4 Kubernetes

```bash
make k8s-render           # revisa os manifestos renderizados
make k8s-apply            # aplica o overlay de dev
kubectl apply -k deploy/k8s/overlays/prod
```

### 4.5 Enderecos

| Endereco | O que e |
| --- | --- |
| `http://localhost:8000/` | console web |
| `http://localhost:8000/api/docs` | Swagger UI |
| `http://localhost:8000/api/redoc` | ReDoc |
| `http://localhost:8000/api/openapi.json` | contrato OpenAPI 3.1 |
| `http://localhost:8000/healthz` | liveness (nao toca em dependencias) |
| `http://localhost:8000/readyz` | readiness (banco, registry, LLM, embeddings, tracer) |
| `http://localhost:8000/metrics` | metricas Prometheus |

`/api/docs` e `/api/redoc` carregam os bundles do Swagger e do ReDoc de um CDN, e a CSP
dessas duas rotas libera exatamente essa origem. Em rede fechada, aponte para o espelho
interno com `LUKATO_APP__DOCS_ASSETS_BASE=https://npm.interno.exemplo/npm`. O contrato em
si (`/api/openapi.json`) nao depende de nada externo.

---

## 5. Uso da aplicacao

Tres interfaces, **um** conjunto de casos de uso. O console web nao tem atalho para o
banco, e a CLI tambem nao: as tres portas de entrada atravessam as mesmas classes de
`application/use_cases/`. O que voce faz pela tela, faz por `curl`, e faz por terminal —
com o mesmo resultado e a mesma trilha de auditoria.

### 5.1 O console web, tela a tela

Layout de tres colunas: **menu recolhivel** (esquerda) · **conteudo** (centro) ·
**painel de contexto** (direita, com o objeto selecionado). Sem bundler, sem framework:
Jinja2 no servidor e ES2020 puro no cliente, carregado com `defer`. Tudo funciona sem
JavaScript — o JS so melhora o que ja funciona.

| Rota | Tela | O que o operador faz ali |
| --- | --- | --- |
| `/` | **Cockpit** | visao geral: execucoes recentes, custo do periodo, saude dos provedores, atalhos |
| `/modules` | **Modulos** | lista as definicoes, filtra por tipo/status/texto, cria e edita |
| `/modules/{slug}` | **Operacao do modulo** | invoca o modulo pela tela, ve a resposta, os achados de guardrail, os passos e o custo |
| `/prompts` | **Prompts** | catalogo versionado; pre-visualiza a renderizacao com variaveis |
| `/guardrails` | **Guardrails** | politicas e regras; **testa uma politica contra um texto** antes de vincular |
| `/knowledge` | **Conhecimento** | ingestao de documentos, colecoes, busca semantica |
| `/runs` | **Execucoes** | historico filtravel por modulo, status e janela de tempo |
| `/runs/{run_id}` | **Detalhe da execucao** | passo a passo, tokens, custo, `trace_id`, achados |
| `/finops` | **FinOps** | custo por modulo/modelo, serie temporal, orcamentos |
| `/identity` | **Identidade** | usuarios, papeis, chaves de API |
| `/observability` | **Observabilidade** | estado do tracer, metricas, saude dos provedores |
| `/registry` | **Registry** | building blocks instalados, capabilities e schema de configuracao |
| `/settings` | **Configuracoes** | configuracao efetiva, com segredos mascarados |
| `/adwatch` | **AdWatch** | painel do funil: capacidades multimodais, midias, deteccao |
| `/adwatch/commercials` | **Catalogo de comerciais** | CRUD do texto conhecido, importacao em lote |
| `/adwatch/detections` | **Deteccoes** | fila de revisao com evidencia por sinal |

Interacoes que vale conhecer:

- **`⌘K` / `Ctrl+K`** abre a paleta de comandos. Ela filtra rotas e modulos por
  subsequencia — digitar `adcom` encontra "AdWatch · Comerciais". As opcoes saem do
  proprio DOM: o que esta no menu esta na paleta, sempre, sem indice a manter.
- **`[`** recolhe e expande o menu lateral. O estado fica em
  `localStorage["lukato.sidebar"]` e e aplicado ja no primeiro paint, sem piscada.
- **Tema claro/escuro** persistido em `localStorage["lukato.theme"]`.
- **Graficos** sao SVG escrito a mao (`charts.js`) — `line`, `area` e `bar`. Sem
  biblioteca, porque o console precisa renderizar identico em rede fechada e um grafico
  de custo por hora e uma polilinha. As cores saem das variaveis CSS, entao os graficos
  acompanham o tema sozinhos.
- **Acoes destrutivas** exigem confirmacao (`data-confirm`).

> **Como o console e a API sao a mesma coisa.** Os formularios HTML do console fazem
> `POST` com `Content-Type: application/x-www-form-urlencoded` para as **mesmas rotas de
> `/api/v1/`** que um cliente HTTP usaria. Um middleware ASGI
> (`interfaces/http/console_forms.py`) traduz o formulario para JSON — inclusive
> reconstruindo objetos aninhados a partir de nomes de campo como
> `binding.temperature` — e converte a resposta em redirecionamento quando o cliente e um
> navegador. Nao existe uma "API do console" paralela: existe uma API, e um tradutor de
> formulario na frente dela.

### 5.2 A API HTTP

Prefixo unico `/api/v1`, declarado em um so lugar. **66 rotas, 91 operacoes.** Erros
seguem um envelope estavel:

```json
{ "error": { "code": "guardrail_violation", "message": "...", "details": { } } }
```

O `code` vem do dominio (`LukatoError.code`) e e **estavel**: cliente pode programar em
cima dele. A tabela completa:

| `code` | HTTP | Quando |
| --- | --- | --- |
| `validation_error` | 422 | corpo ou parametro fora do schema |
| `guardrail_violation` | 422 | uma regra com acao `block` disparou |
| `unauthorized` | 401 | sem credencial valida |
| `forbidden` | 403 | credencial valida, permissao insuficiente |
| `not_found` / `module_not_found` | 404 | recurso inexistente |
| `conflict` | 409 | slug duplicado, modulo em `draft`, estado incompativel |
| `budget_exceeded` | 402 | orcamento com `hard_stop` estourado |
| `rate_limited` | 429 | limite de requisicoes |
| `unsupported_capability` | 501 | capacidade nao instalada (ex.: ASR sem WhisperX) |
| `provider_error` | 502 | falha do hub de LLM ou de embeddings |
| `module_error` / `configuration_error` / `lukato_error` | 500 | falha interna |

Listagens paginam com `limit` (1..200, padrao 50) e `offset`.

**Middlewares**, na ordem em que a requisicao os atravessa: `cors` → `request_id` →
`security_headers` → `rate_limit` → `timing` → `console_form`.

- `X-Request-ID` e gerado ou propagado do cliente, injetado no contexto de log e
  carimbado tambem nas respostas de erro — o mesmo identificador liga log, metrica,
  trace e run;
- `X-Response-Time-ms` em toda resposta, e a mesma medicao alimenta o Prometheus;
- cabecalhos de seguranca: `Content-Security-Policy`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Permissions-Policy` e HSTS
  quando em HTTPS;
- limite de **240 requisicoes por 60 s** por padrao, com `429`, `Retry-After`,
  `X-RateLimit-Limit` e `X-RateLimit-Window`. Rotas de saude e metricas sao isentas.

#### Mapa das rotas

<details>
<summary><b>Sistema, registry, modulos e execucoes</b></summary>

```
GET    /healthz                             liveness (Kubernetes)
GET    /readyz                              readiness: banco, registry, llm, embeddings, tracer
GET    /metrics                             metricas Prometheus
GET    /api/v1/health/live                  liveness da aplicacao
GET    /api/v1/health/ready                 prontidao da instalacao
GET    /api/v1/health/providers             detalhe dos provedores externos

GET    /api/v1/registry                     building blocks instalados
GET    /api/v1/registry/{slug}              descreve um building block
POST   /api/v1/registry/discover            reexecuta a descoberta por entry point

GET    /api/v1/modules                      lista definicoes (kind, status, search, limit, offset)
POST   /api/v1/modules                      cria uma definicao
GET    /api/v1/modules/{slug}               busca uma definicao
PUT    /api/v1/modules/{slug}               atualiza a definicao (troca a trinca sem redeploy)
PATCH  /api/v1/modules/{slug}/status        draft | active | inactive
DELETE /api/v1/modules/{slug}               remove a definicao
POST   /api/v1/modules/{slug}/invoke        invoca pela trinca
POST   /api/v1/modules/{slug}/dry-run       ensaia SEM chamar o provedor

GET    /api/v1/runs                         module_slug, status, since, until, tenant_id
GET    /api/v1/runs/{run_id}                execucao
GET    /api/v1/runs/{run_id}/steps          passos da execucao
POST   /api/v1/runs/{run_id}/cancel         cancela uma execucao
```

</details>

<details>
<summary><b>Prompts e guardrails</b></summary>

```
GET    /api/v1/prompts                      lista prompts
POST   /api/v1/prompts                      cria prompt
GET    /api/v1/prompts/{prompt_id}          obtem prompt
PUT    /api/v1/prompts/{prompt_id}          atualiza prompt
DELETE /api/v1/prompts/{prompt_id}          remove prompt
GET    /api/v1/prompts/slug/{slug}          obtem por slug
GET    /api/v1/prompts/slug/{slug}/versions lista versoes
POST   /api/v1/prompts/{prompt_id}/clone    clona uma versao
POST   /api/v1/prompts/{prompt_id}/preview  pre-visualiza a renderizacao

GET    /api/v1/guardrails                   lista politicas
POST   /api/v1/guardrails                   cria politica
GET    /api/v1/guardrails/{policy_id}       obtem politica
PUT    /api/v1/guardrails/{policy_id}       atualiza politica
DELETE /api/v1/guardrails/{policy_id}       remove politica
GET    /api/v1/guardrails/slug/{slug}       obtem por slug
GET    /api/v1/guardrails/rule-kinds        catalogo dos 11 tipos de regra, com JSON Schema
POST   /api/v1/guardrails/test              testa uma politica (salva ou avulsa) contra um texto
```

</details>

<details>
<summary><b>Conhecimento, FinOps e identidade</b></summary>

```
POST   /api/v1/knowledge/documents                       ingere documento
GET    /api/v1/knowledge/documents                       lista documentos
GET    /api/v1/knowledge/documents/{document_id}         obtem documento
DELETE /api/v1/knowledge/documents/{document_id}         remove documento
POST   /api/v1/knowledge/documents/{document_id}/reindex reindexa documento
POST   /api/v1/knowledge/search                          busca semantica (query, collection, limit, filters, rerank)
GET    /api/v1/knowledge/collections                     colecoes existentes
GET    /api/v1/knowledge/health                          saude da base

GET    /api/v1/finops/summary                            resumo de custo do periodo
GET    /api/v1/finops/series                             serie temporal de custo
GET    /api/v1/finops/usage                              registros de consumo
GET    /api/v1/finops/prices                             tabela de precos por modelo
PUT    /api/v1/finops/prices                             atualiza a tabela
GET    /api/v1/finops/budgets                            lista orcamentos
POST   /api/v1/finops/budgets                            cria orcamento
GET    /api/v1/finops/budgets/{budget_id}                obtem orcamento
PUT    /api/v1/finops/budgets/{budget_id}                atualiza orcamento
DELETE /api/v1/finops/budgets/{budget_id}                remove orcamento
GET    /api/v1/finops/budgets/{budget_id}/status         situacao corrente (consumido x teto)

POST   /api/v1/identity/login                            autentica por e-mail e senha
POST   /api/v1/identity/token/refresh                    renova o token
GET    /api/v1/identity/me                               identidade corrente
GET    /api/v1/identity/users                            lista usuarios
POST   /api/v1/identity/users                            cria usuario
GET    /api/v1/identity/users/{user_id}                  obtem usuario
PUT    /api/v1/identity/users/{user_id}                  atualiza usuario
DELETE /api/v1/identity/users/{user_id}                  remove usuario
POST   /api/v1/identity/users/{user_id}/password         troca a senha
GET    /api/v1/identity/api-keys                         lista chaves de API
POST   /api/v1/identity/api-keys                         cria chave (o segredo aparece uma unica vez)
POST   /api/v1/identity/api-keys/{api_key_id}/rotate     rotaciona a chave
DELETE /api/v1/identity/api-keys/{api_key_id}            revoga a chave
```

</details>

<details>
<summary><b>AdWatch</b></summary>

```
GET    /api/v1/adwatch/capabilities                      o que esta instalado, pesos, limiares, teto de score
POST   /api/v1/adwatch/commercials                       cria comercial
GET    /api/v1/adwatch/commercials                       lista comerciais
POST   /api/v1/adwatch/commercials/bulk                  importa em lote
GET    /api/v1/adwatch/commercials/{commercial_id}       detalha
PUT    /api/v1/adwatch/commercials/{commercial_id}       atualiza (reassina o fingerprint)
DELETE /api/v1/adwatch/commercials/{commercial_id}       remove
POST   /api/v1/adwatch/media                             registra midia por URI
POST   /api/v1/adwatch/media/upload                      envia o arquivo e registra o ativo
GET    /api/v1/adwatch/media                             lista midias
GET    /api/v1/adwatch/media/{media_id}                  detalha
POST   /api/v1/adwatch/media/{media_id}/ingest           executa a ingestao possivel (FFmpeg/WhisperX/OCR/cenas)
POST   /api/v1/adwatch/media/{media_id}/transcript       importa transcricao (JSON WhisperX)
GET    /api/v1/adwatch/media/{media_id}/transcript       le a transcricao; `?q=` busca frase exata
POST   /api/v1/adwatch/media/{media_id}/scenes           importa cortes de cena
POST   /api/v1/adwatch/media/{media_id}/ocr              importa textos de OCR
POST   /api/v1/adwatch/media/{media_id}/detect           executa o funil de deteccao
GET    /api/v1/adwatch/media/{media_id}/detections       deteccoes de uma midia
GET    /api/v1/adwatch/detections                        busca (media_id, commercial_id, status, limit, offset)
GET    /api/v1/adwatch/detections/{detection_id}         detalha com evidencia por sinal
PATCH  /api/v1/adwatch/detections/{detection_id}         revisao humana: aceita, rejeita, ajusta bordas
```

</details>

### 5.3 A CLI

Dez comandos, todos sobre os mesmos casos de uso da API. A CLI mantem o log em `WARNING`
por padrao para que `stdout` fique limpo para `jq` e para pipes; `-v` eleva ao nivel de
`LUKATO_OBSERVABILITY__LOG_LEVEL`.

```
lukato serve      sobe a API + console com uvicorn   [--host --port --reload]
lukato seed       popula prompts, guardrails, modulos e catalogo demo   [--reset]
lukato openapi    exporta o contrato OpenAPI 3.1     --out CAMINHO
lukato export     grava em JSON prompts, guardrails, modulos, comerciais e midias
lukato reindex    reassina o catalogo de comerciais com o embedder atual
lukato import     recria nesta instalacao o que um `lukato export` levou de outra
lukato health     imprime o relatorio de prontidao em JSON
lukato modules    list | show | invoke
lukato adwatch    detect  [--media ID] [--keep-rejected] [--json]
lukato version    imprime a versao do pacote
```

Codigos de saida: `0` sucesso, `1` erro de dominio/configuracao/instalacao nao pronta,
`130` interrompido pelo operador (128 + SIGINT).

O `seed` e **idempotente**: rodar duas vezes nao duplica nada e nao falha. Ele existe
para que uma instalacao recem-criada ja tenha a trinca configurada, dois agentes
diferentes sobre a mesma classe `processing` e um caso de AdWatch pronto para detectar.
A senha do usuario root nunca vem do codigo: ou o operador informa por
`LUKATO_SEED_ROOT_PASSWORD`, ou o seed sorteia uma com `secrets` e a imprime **uma unica
vez**.

`export`/`import` movem uma instalacao inteira entre ambientes; `import` le da entrada
padrao, entao `lukato export | ssh outro-host lukato import` funciona.

---

### 5.4 Receita 1 — criar um agente sem escrever codigo

```bash
# 1. escolha as pecas ja versionadas
curl -s localhost:8000/api/v1/guardrails/slug/entrada-estrita | jq -r .id   # -> IN
curl -s localhost:8000/api/v1/prompts/slug/triagem-atendimento | jq -r .id  # -> PROMPT
curl -s localhost:8000/api/v1/guardrails/slug/saida-auditada  | jq -r .id   # -> OUT

# 2. crie a definicao — este e o "deploy" de um agente novo
curl -s -X POST localhost:8000/api/v1/modules \
  -H 'Content-Type: application/json' -d '{
    "slug": "triagem-fibra",
    "name": "Triagem de fibra",
    "kind": "agent",
    "runtime": "langgraph",
    "binding": {
      "input_guardrail_id":  "IN",
      "system_prompt_id":    "PROMPT",
      "output_guardrail_id": "OUT",
      "model": "qwen-latest",
      "temperature": 0.0,
      "max_tokens": 512,
      "tools": ["knowledge_search"]
    },
    "config": { "module": "processing" },
    "status": "active"
  }' | jq

# 3. use
curl -s -X POST localhost:8000/api/v1/modules/triagem-fibra/invoke \
  -H 'Content-Type: application/json' \
  -d '{"input":"minha fibra caiu de novo","variables":{"canal":"voz"}}' | jq
```

Trocar a politica de seguranca depois, **em producao, sem redeploy**:

```bash
curl -s -X PUT localhost:8000/api/v1/modules/triagem-fibra \
  -H 'Content-Type: application/json' \
  -d '{"binding":{"input_guardrail_id":"<outra politica>"}}' | jq
```

### 5.5 Receita 2 — ensaiar antes de gastar (`dry-run`)

`POST /api/v1/modules/{slug}/dry-run` percorre a resolucao inteira **sem chamar o
provedor**: aplica o guardrail de entrada, renderiza o system prompt, monta o plano de
execucao e devolve tudo. Serve para revisar uma configuracao nova sem custo e sem risco.

```bash
curl -s -X POST localhost:8000/api/v1/modules/triagem/dry-run \
  -H 'Content-Type: application/json' -d '{"input":"minha internet caiu"}' | jq
```

```jsonc
{
  "module_slug": "triagem", "module_status": "active",
  "invocable": true, "allowed": true, "would_call_provider": true,
  "input_guardrail": { "allowed": true, "blocked": false, "modified": false,
                       "findings": [], "latency_ms": 4.02 },
  "system_prompt": { "bound": true, "slug": "triagem-atendimento", "version": 1,
                     "rendered": "Voce e o analista de triagem...",
                     "variables": [], "missing": [], "complete": true },
  "plan": { "runtime": "direct", "model": "qwen-latest", "temperature": 0.0, "max_tokens": 512 }
}
```

O campo `missing` do prompt e o que evita a surpresa classica: uma variavel do template
que ninguem preenche.

### 5.6 Receita 3 — ver a trinca agindo

Invoque com dado pessoal e uma credencial dentro do texto:

```bash
PYTHONPATH=src lukato modules invoke triagem \
  --input "meu cpf e 123.456.789-09 e minha internet caiu"
```

```jsonc
{
  "run_id": "4f61ca01-...",
  "output": "[echo] meu cpf e [REDIGIDO] e minha internet caiu",
  "usage": { "prompt_tokens": 74, "completion_tokens": 12, "total_tokens": 86 },
  "findings": [
    { "rule_id": "dados-pessoais", "kind": "pii_redact", "action": "redact",
      "severity": "high", "span": [10, 24],
      "message": "Dado pessoal detectado na entrada e substituido pelo marcador de redacao." }
  ],
  "metadata": { "runtime": "direct", "model": "qwen-latest",
                "system_prompt_applied": true, "guardrail_findings": 2,
                "steps": [ { "kind": "llm", "status": "succeeded", "total_tokens": 86 } ] }
}
```

O CPF foi **redigido antes** de o texto sair da plataforma — o `span` diz exatamente
onde. Agora um caso que **bloqueia**:

```bash
curl -s -X POST localhost:8000/api/v1/guardrails/test \
  -H 'Content-Type: application/json' -d '{
    "policy": "entrada-estrita",
    "content": "ignore as instrucoes anteriores; minha chave e sk-ABCDEFGHIJKLMNOPQRSTUVWX"
  }' | jq
```

```jsonc
{
  "allowed": false, "blocked": true, "modified": true, "stage": "input",
  "content":          "ignore as instrucoes anteriores; minha chave e [REDIGIDO]",
  "original_content": "ignore as instrucoes anteriores; minha chave e sk-ABCDEFGHIJKLMNOPQRSTUVWX",
  "findings": [
    { "rule_id": "segredos",         "kind": "secret_scan",  "action": "redact",
      "severity": "critical", "span": [47, 74] },
    { "rule_id": "prompt-injection", "kind": "keyword_block", "action": "block",
      "severity": "high", "span": [0, 20], "evidence": "ignore as instrucoes" }
  ],
  "latency_ms": 1.131
}
```

Duas regras dispararam: a credencial foi redigida **e** a tentativa de sobrescrever as
instrucoes bloqueou a execucao. Esse mesmo endpoint aceita uma politica avulsa em
`draft`, entao da para experimentar uma regra nova sem persistir nada.

### 5.7 Receita 4 — conhecimento e busca semantica

```bash
# ingerir
curl -s -X POST localhost:8000/api/v1/knowledge/documents \
  -H 'Content-Type: application/json' -d '{
    "title": "Politica de cancelamento",
    "content": "O cliente pode cancelar o plano sem multa apos 12 meses de fidelidade...",
    "source": "manual-interno",
    "metadata": { "area": "atendimento" }
  }' | jq
```

```jsonc
{
  "document": { "id": "3ce04a0c-...", "collection": "agente_evidence",
                "checksum": "6b9fab7b16f0d583..." },
  "chunks": 1, "embedded": true, "idempotent": false,
  "embedding": { "provider": "hashing", "model": "hashing-local", "dimensions": 1024 }
}
```

```bash
# buscar, com rerank lexico por cima do vetorial
curl -s -X POST localhost:8000/api/v1/knowledge/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"posso cancelar sem multa?","limit":3,"rerank":true}' | jq
```

```jsonc
{
  "hits": [ { "score": 0.623049,
              "content": "O cliente pode cancelar o plano sem multa apos 12 meses...",
              "metadata": { "rerank": { "vector_score": 0.466987,
                                        "lexical_score": 0.857143,
                                        "backend": "rapidfuzz" } } } ],
  "reranked": true
}
```

O `checksum` torna a ingestao idempotente: reenviar o mesmo conteudo devolve
`idempotent: true` em vez de duplicar. E cada chunk carrega o `embedding_provider`,
`embedding_model` e `embedding_dimensions` que o produziram — e por isso que a colecao
consegue recusar uma escrita divergente (secao 9.3).

Para dar a base a um agente, basta acrescentar `"knowledge_search"` a lista `tools` do
binding. Nenhuma linha de codigo.

### 5.8 Receita 5 — detectar um comercial em horas de video

```bash
# 1. catalogo (o texto conhecido)
curl -s -X POST localhost:8000/api/v1/adwatch/commercials \
  -H 'Content-Type: application/json' -d '{
    "commercial_id": "COM_000234", "brand": "Claro", "campaign": "Pos 100GB",
    "text": "Chegou o Claro Pos com muito mais internet...",
    "duration_expected": 30, "keywords": ["Claro","internet"],
    "key_phrases": ["muito mais internet"]
  }' | jq

# 2. midia (por URI, ou upload do arquivo)
curl -s -X POST localhost:8000/api/v1/adwatch/media \
  -H 'Content-Type: application/json' \
  -d '{"uri":"file:///dados/programa.mp4","kind":"video","title":"Programa"}' | jq
curl -s -X POST localhost:8000/api/v1/adwatch/media/upload \
  -F 'file=@programa.mp4' -F 'kind=video' -F 'title=Programa'

# 3a. com FFmpeg + WhisperX instalados
curl -s -X POST localhost:8000/api/v1/adwatch/media/$MEDIA/ingest | jq
# 3b. ou importando a transcricao — sem FFmpeg, sem GPU, sem rede.
#     Aceita a lista direta de palavras...
curl -s -X POST localhost:8000/api/v1/adwatch/media/$MEDIA/transcript \
  -H 'Content-Type: application/json' \
  -d '[{"word":"chegou","start":122.3,"end":122.6}]' | jq
#     ...ou o JSON do WhisperX inteiro, ou o proprio arquivo .json
curl -s -X POST localhost:8000/api/v1/adwatch/media/$MEDIA/transcript \
  -H 'Content-Type: application/json' \
  -d '{"language":"pt","segments":[{"words":[{"word":"chegou","start":122.3,"end":122.6}]}]}'
curl -s -X POST localhost:8000/api/v1/adwatch/media/$MEDIA/transcript -F 'file=@whisperx.json'

# 4. detectar
curl -s -X POST localhost:8000/api/v1/adwatch/media/$MEDIA/detect | jq

# 5. revisar
curl -s "localhost:8000/api/v1/adwatch/detections?status=needs_review" | jq
curl -s -X PATCH localhost:8000/api/v1/adwatch/detections/$DET \
  -H 'Content-Type: application/json' -d '{"status":"accepted"}' | jq
```

Pela CLI, o mesmo funil em uma linha:

```
$ lukato adwatch detect
COMERCIAL       INICIO       FIM   CONF  STATUS
COM_000234       122.3     150.0  0.777  needs_review

janelas=172 candidatos=99 comerciais=5 persistidas=1 substituidas=0
aceitas=0 revisao=1 rejeitadas=0 vlm=sim semantico=sim tempo=95ms
```

Buscar uma frase exata na transcricao, com os tempos:

```bash
curl -s "localhost:8000/api/v1/adwatch/media/$MEDIA/transcript?q=muito%20mais%20internet" | jq
```

### 5.9 Receita 6 — controlar o custo antes da fatura

```bash
# preco por 1k tokens, por modelo
curl -s -X PUT localhost:8000/api/v1/finops/prices \
  -H 'Content-Type: application/json' \
  -d '{"prices":[{"model":"qwen-latest","input_usd_per_1k":0.0004,"output_usd_per_1k":0.0012}]}' | jq

# orcamento com corte duro
curl -s -X POST localhost:8000/api/v1/finops/budgets \
  -H 'Content-Type: application/json' \
  -d '{"name":"triagem-mensal","scope":"module:triagem","period":"monthly",
       "limit_usd":50.0,"alert_threshold":0.8,"hard_stop":true}' | jq

curl -s localhost:8000/api/v1/finops/summary | jq
curl -s localhost:8000/api/v1/finops/budgets/$ID/status | jq
```

O `scope` e uma string: `global`, `module:<slug>` ou `tenant:<id>`. O `period` aceita
`daily`, `weekly`, `monthly` e `total`. `alert_threshold` (padrao 0.8) e a fracao do teto
a partir da qual o orcamento passa a alertar.

Com `hard_stop: true`, a **etapa 4** de `InvokeModule` recusa a execucao com
`budget_exceeded` (HTTP 402) — antes do guardrail, antes do prompt, antes do provedor. O
estouro vira uma recusa explicita e auditavel, nao uma linha na fatura no mes seguinte.

### 5.10 Receita 7 — auditar o que aconteceu

```bash
curl -s "localhost:8000/api/v1/runs?module_slug=triagem&status=blocked&since=2026-09-01T00:00:00Z" | jq
curl -s localhost:8000/api/v1/runs/$RUN/steps | jq
```

Toda invocacao — bem-sucedida, bloqueada ou falha — vira um `AgentRun` persistido com
`steps`, tokens, custo e `trace_id`.

`RunStatus`: `pending` · `running` · `succeeded` · `failed` · `blocked` · `cancelled`.
`StepKind`: `guardrail_in` · `prompt` · `llm` · `tool` · `retrieval` · `plan` ·
`reflect` · `guardrail_out` · `error`.

A leitura mais util e a sequencia de `StepKind`. Um run bem-sucedido no runtime `direct`
tem `['guardrail_in', 'prompt', 'llm', 'guardrail_out']`. Um run **bloqueado na entrada
tem exatamente um passo: `['guardrail_in']`** — nao ha `prompt`, nao ha `llm`. Provar que
o provedor nao foi chamado e ler uma lista, nao inspecionar log de rede.

O texto gravado em cada passo e recortado em 4000 caracteres: a trilha e auditoria, nao
armazenamento do dado.

### 5.11 Receita 8 — mover a instalacao entre ambientes

```bash
lukato export > instalacao.json        # prompts, guardrails, modulos, comerciais, midias
lukato import < instalacao.json        # recria do outro lado
lukato reindex                         # reassina o catalogo com o embedder atual
```

`reindex` e obrigatorio depois de trocar o provedor ou o modelo de embeddings: as
assinaturas semanticas do catalogo de comerciais precisam viver no mesmo espaco vetorial
da consulta (secao 9.3).

---

## 6. Building blocks: criar, versionar, distribuir

### 6.1 Os cinco modulos embutidos

| slug | tipo | capabilities declaradas | o que faz |
| --- | --- | --- | --- |
| `auth` | auth | `login` `issue_token` `api_keys` `rbac` | JWT, chaves de API, papeis `root`/`admin`/`operator`/`viewer` |
| `processing` | agent | `chat` `structured_output` `tools` `streaming` | agente generico — **todo** o comportamento vem do binding |
| `finops` | finops | `cost_summary` `budgets` `forecast` | custo por modulo/modelo/tenant, orcamentos, alertas e bloqueio |
| `knowledge` | knowledge | `ingest` `chunk` `embed` `semantic_search` | chunking, embeddings Qwen, busca semantica com pgvector |
| `adwatch` | pipeline | `crud_commercials` `ingest_media` `detect` `review` | catalogo de comerciais e deteccao temporal multimodal |

`GET /api/v1/registry` devolve, para cada building block, as capabilities, a versao e o
**JSON Schema da configuracao aceita** — e o que permite ao console montar o formulario
certo sem conhecer o modulo de antemao.

### 6.2 Caminho A — sem codigo (o caminho normal)

Crie uma `ModuleDefinition` sobre a classe generica `processing`. E a secao 5.4 inteira.
Este e o caminho para 90% dos casos: um agente novo e configuracao.

Tipos possiveis (`kind`): `agent` · `tool` · `pipeline` · `auth` · `finops` ·
`knowledge` · `custom`.

Ciclo de vida (`status`): `draft` → `active` → `paused` → `deprecated`, por
`PATCH /api/v1/modules/{slug}/status`. **Somente `active` e invocavel**: a etapa 2 de
`InvokeModule` recusa qualquer outro estado com `409 conflict` — verificado em
`prova_trinca.py`, asercao 5. Um agente novo pode nascer em `draft`, ser revisado com
`dry-run` e so entao ir para `active`.

### 6.3 Caminho B — com codigo (quando ha logica propria de verdade)

```python
from lukato.modules.base import BaseModule, ModuleRequest, ModuleResponse, UIDescriptor, UINavItem
from lukato.modules.registry import register_module
from lukato.domain.models.module import ModuleKind

@register_module
class MeuModulo(BaseModule):
    slug = "meu-modulo"
    name = "Meu modulo"
    kind = ModuleKind.AGENT
    capabilities = ("chat",)
    config_schema = {"type": "object", "properties": {"limite": {"type": "integer"}}}

    async def setup(self, ctx) -> None: ...          # opcional
    async def teardown(self) -> None: ...            # opcional

    async def handle(self, request: ModuleRequest, ctx) -> ModuleResponse:
        # ctx entrega portas ja resolvidas: llm, embeddings, guardrails, tracer,
        # uow_factory, orchestrators, settings — e a fachada
        # ctx.services["pipeline"], que ja recebe o system prompt renderizado
        # e chama o runtime declarado no binding.
        ...

    def ui(self) -> UIDescriptor:                    # opcional
        return UIDescriptor(nav=[UINavItem(label="Meu modulo", icon="cube",
                                           endpoint="/meu-modulo")])
```

Tres pontos importam aqui:

1. **O modulo nao instancia cliente de LLM** — recebe a porta pelo `ModuleContext`. E o
   que impede que a trinca vire opcional: nao existe caminho alternativo para chamar o
   provedor.
2. **`config_schema` e contrato publico.** O registry o expoe, e o console monta o
   formulario de configuracao a partir dele — sem conhecer o modulo.
3. **`ui()` publica menu e templates no console.** Um building block de terceiros
   aparece na navegacao, com pagina central e painel de contexto proprios, sem tocar em
   nada da plataforma. Ha teste para isso
   (`test_modulo_de_fora_publica_item_de_menu_no_console`), e tambem para o caso
   inverso: remover o modulo do registry **nao derruba a aplicacao**.

`ModuleRequest` carrega `input`, `payload`, `variables`, `history` e `stream`;
`ModuleResponse` devolve `output`, `data`, `run_id`, `usage`, `cost_usd`, `findings` e
`metadata`.

Distribua como pacote com entry point no grupo `lukato.modules`:

```toml
[project.entry-points."lukato.modules"]
meu-modulo = "meu_pacote.modulo:MeuModulo"
```

O nucleo descobre sozinho — `POST /api/v1/registry/discover` reexecuta a varredura **sem
reiniciar o processo**. Nenhuma alteracao no codigo da plataforma.

---

## 7. Guardrails

### 7.1 Os onze tipos de regra

| tipo | o que faz | acoes suportadas |
| --- | --- | --- |
| `regex_block` | dispara quando qualquer regex da lista casa | allow, warn, redact, transform, block |
| `regex_require` | dispara quando uma regex obrigatoria **nao** aparece | allow, warn, block |
| `keyword_block` | termos proibidos, comparando sem acento e sem caixa | allow, warn, redact, transform, block |
| `pii_redact` | CPF, CNPJ e cartao **com digito verificador conferido** | allow, warn, redact, transform, block |
| `secret_scan` | `sk-`, `AKIA`, `ghp_`, JWT, `Bearer`, chaves privadas PEM | allow, warn, redact, transform, block |
| `max_length` | teto de caracteres e/ou tokens estimados | allow, warn, transform, block |
| `min_length` | minimo de caracteres | allow, warn, block |
| `json_schema` | exige JSON valido conforme o schema informado | allow, warn, block |
| `language_allow` | idioma por heuristica de stopwords | allow, warn, block |
| `topic_block` | densidade de termos: dispara ao atingir o limiar | allow, warn, redact, transform, block |
| `llm_judge` | veredito estruturado de um LLM sobre o criterio | allow, warn, block |

`GET /api/v1/guardrails/rule-kinds` devolve esta tabela **com o JSON Schema de
configuracao de cada tipo** — e o que permite ao console montar o formulario certo para
cada regra sem hardcode.

Detalhe deliberado do `pii_redact`: conferir o digito verificador elimina o falso
positivo classico de um numero de protocolo de 11 digitos virar "CPF".

Detalhe deliberado do `llm_judge`: falha do provedor vira **aviso**, nao bloqueio — um
juiz indisponivel nao pode derrubar a plataforma. Use-o sempre como ultima regra da
politica.

### 7.2 Acoes e severidades

Acoes: `allow` · `warn` · `redact` · `transform` · `block`.
Severidades: `low` · `medium` · `high` · `critical`.

O motor e determinista e encadeado:

1. as regras habilitadas sao avaliadas na ordem `(order, id)` — nunca na ordem de
   insercao, nunca em paralelo;
2. **o conteudo de cada regra e o resultado da anterior**: as redacoes se acumulam. Foi
   assim que, na secao 5.6, a credencial foi redigida por `secret_scan` e a regra seguinte
   ja viu o texto sem ela;
3. `redact` e `transform` alteram o texto e a cadeia **continua**;
4. `block` interrompe **na hora**: `GuardrailViolation`, HTTP 422, `AgentRun(BLOCKED)`. As
   regras posteriores nao sao avaliadas — nao ha custo depois da decisao;
5. todos os achados sao persistidos no run, inclusive os que nao bloquearam.

E o motor tem uma politica explicita para os proprios defeitos. Uma regra sem avaliador
registrado, ou um avaliador que levanta excecao, **nao e ignorada**: com `fail_open=false`
(o padrao) vira `UnsupportedCapability` ou `GuardrailViolation`; com `fail_open=true`
vira um achado de aviso e a cadeia segue. Nos dois casos o fato fica escrito. O que nao
existe e o caminho silencioso — uma regra de seguranca que nao rodou nunca passa
despercebida.

`fail_open` e configuravel por politica **e** globalmente
(`LUKATO_GUARDRAILS__FAIL_OPEN`, padrao `false`).

### 7.3 As politicas que o seed entrega

| slug | estagio | regras |
| --- | --- | --- |
| `entrada-padrao` | input | `max_length` · `secret_scan` · `pii_redact` · `keyword_block` (prompt injection) |
| `entrada-estrita` | input | as quatro acima + `language_allow` · `topic_block` |
| `saida-padrao` | output | `secret_scan` · `pii_redact` · `max_length` |
| `saida-json` | output | `json_schema` · `max_length` |
| `saida-auditada` | output | `secret_scan` · `pii_redact` · `max_length` · `llm_judge` |

Um bloqueio no guardrail de **entrada** acontece **antes** de qualquer chamada ao
provedor: o texto barrado nunca sai da plataforma.

---

## 8. Runtimes de agente e ferramentas

### 8.1 Tres runtimes, uma porta

| runtime | quando usar | como funciona |
| --- | --- | --- |
| `direct` | resposta unica, latencia minima | uma chamada de LLM, um passo `llm` no run |
| `langgraph` | raciocinio em varias etapas com ferramentas | grafo de estado com seis nos |
| `deepagent` | tarefas longas, subagentes, planejamento | harness `deepagents`; so entra quando as bibliotecas estao instaladas **e** ha credencial |

O grafo do runtime `langgraph`:

```
START → prepare ─┬→ plan → act ─┬→ observe ─┬→ act        (laco de ferramentas)
                 │              │           └→ reflect
                 └────────────→ act         └→ reflect
                                                  ↓
                                              finalize → END
```

`prepare` decide se ha planejamento; `act` chama o modelo ou uma ferramenta; `observe`
processa o resultado e decide se volta a `act`; `reflect` fecha o raciocinio;
`finalize` monta a resposta. O numero de iteracoes e limitado por
`config.max_iterations` do modulo, e o run registra cada no como um `RunStep`
(`plan`, `act`, `observe`, `reflect`), com latencia e tokens por passo.

A escolha do runtime esta no binding — `"runtime": "langgraph"`. Trocar de runtime e
trocar uma string, sem redeploy.

### 8.2 As ferramentas

Cinco, declaradas por nome na lista `tools` do binding:

| ferramenta | o que faz |
| --- | --- |
| `knowledge_search` | busca semantica na base de conhecimento |
| `commercial_lookup` | consulta o catalogo de comerciais do AdWatch |
| `cost_lookup` | consulta o consumo/custo acumulado |
| `calculator` | aritmetica com avaliacao restrita (sem `eval` aberto) |
| `now` | data e hora corrente |

Uma ferramenta cujo pre-requisito nao existe (por exemplo `knowledge_search` sem
provedor de embeddings) devolve um resultado explicito de indisponibilidade — nao
levanta excecao e nao inventa resposta.

### 8.3 O adaptador de LLM

`OpenAICompatibleLLM` fala com qualquer endpoint compativel com a API da OpenAI —
`LUKATO_LLM__BASE_URL`. Timeout de 60 s e ate 3 tentativas com backoff exponencial
(`LUKATO_LLM__TIMEOUT`, `LUKATO_LLM__MAX_RETRIES`). Ha um `fallback_model`
(`openai/gpt-oss-20b`) para o caso de o modelo principal nao atender.

`EchoLLM` e o irmao deterministico: devolve a entrada prefixada com `[echo]` e contabiliza
tokens de forma estavel. Ele entra automaticamente quando falta credencial, e o motivo
aparece no log e em `/readyz`:

```
LUKATO_LLM__API_KEY ausente: sem credencial nao ha como falar com o hub,
entao o adaptador deterministico offline assume no lugar
```

---

## 9. Conhecimento e embeddings

### 9.1 O caminho

```
documento → normalizacao → chunking → embeddings (lote de 32) → colecao pgvector
                                                                      ↓
consulta → embedding da consulta → HNSW (cosseno) → top-k → [rerank lexico] → trechos
```

**Chunking**: janelas de ate **1200 caracteres** com **200 de sobreposicao**. O corte nao
e cego: procura o separador mais forte disponivel (`\n\n`, depois `\n`, depois `". "`,
depois espaco) na **segunda metade** da janela, e so corta no limite exato quando nenhum
deles existe. O ultimo trecho e descartado quando ja cabe inteiro dentro da sobreposicao
do anterior — o que evitaria um fragmento redundante competindo na busca. A sobreposicao
e limitada a metade do tamanho do chunk; acima disso, a configuracao e recusada.

O `rerank` opcional combina o score vetorial com similaridade lexica (`rapidfuzz`, com
irmao `difflib` quando a biblioteca nao esta instalada) — util quando a consulta tem
termos literais que o vetor dilui, como um numero de contrato. Os dois scores parciais
viajam no metadado do hit (`vector_score`, `lexical_score`, `backend`), entao da para
auditar por que um trecho subiu na lista.

### 9.2 A colecao

Nome padrao `agente_evidence`, 1024 dimensoes, indice HNSW com `vector_cosine_ops` no
PostgreSQL. Em SQLite, a mesma modelagem com cosseno calculado em memoria — o que faz a
suite rodar sem PostgreSQL.

### 9.3 A assimetria deliberada: embeddings nao degradam sozinhos

LLM, tracer e banco caem para o modo deterministico automaticamente. **Embeddings nao.**
A razao esta no ADR-0003 e vale repetir aqui, porque e uma decisao contraintuitiva:

> Uma resposta de LLM degradada e **transitoria** — some no proximo request. Um embedding
> degradado e **persistido**. `HashingEmbedder` e `Qwen3-Embedding-0.6B` produzem vetores
> de 1024 dimensoes em espacos semanticos completamente diferentes; grava-los na mesma
> colecao nao levanta erro em lugar nenhum, so faz a busca devolver resultados errados
> **para sempre**.

Cair para hashing por causa de uma queda temporaria do hub trocaria uma indisponibilidade
visivel por uma corrupcao invisivel. Com `provider=qwen`, portanto, hub fora do ar e
`ProviderError`. E, como o dano seria silencioso, a plataforma **nao confia na
configuracao**: cada colecao registra o provider, o modelo e a dimensao que a produziram,
e **recusa** escrita e leitura divergentes (SPEC-0007 secao 1.2). Trocar de embedder exige
`lukato reindex`.

---

## 10. AdWatch: deteccao temporal multimodal

### 10.1 A inversao do problema

A abordagem obvia seria enviar o video a um modelo multimodal e perguntar onde estao os
comerciais. Em milhares de horas isso e proibitivo, lento e nao explicavel.

Mas **o texto dos comerciais ja e conhecido** — existe um catalogo, com CRUD. Isso muda a
natureza do problema: nao e classificacao aberta de video, e **matching temporal
multimodal**, majoritariamente *retrieval* e alinhamento. O texto conhecido funciona como
supervisao explicita, e o modelo multimodal entra apenas no fim do funil, como juiz da
faixa de incerteza.

O efeito: o custo cai por ordens de grandeza, cada deteccao carrega evidencia auditavel
por sinal, e o pipeline roda **sem GPU** quando ha transcricao.

### 10.2 O funil

```
VIDEO ──┬── audio ──→ ASR (WhisperX) ──→ palavras + timestamps ──┐
        └── frames ─→ scene detect + OCR ─→ texto na tela ───────┤
                                                                 ▼
                                          LINHA DO TEMPO MULTIMODAL
                                                                 ▼
                              janelas deslizantes 15/30/60 s, passo 5 s
                                                                 ▼
                    retrieval sobre fingerprints do catalogo → TOP-K (10)
                                                                 ▼
                                          rerank (TOP-K 3) + fusao de sinais
                                                                 ▼
                                S = 0.40·lexico + 0.25·semantico
                                  + 0.15·ocr + 0.15·visual + 0.05·duracao
                                                                 ▼
                  S ≥ 0.90 aceita │ 0.60 ≤ S < 0.90 juiz Qwen-VL │ S < 0.60 rejeita
                                                                 ▼
                        NMS por IoU > 0.5 → refino de fronteira por cortes de cena
                                                                 ▼
                                       Detection persistida com evidencia por sinal
```

### 10.3 A ingestao nao e tudo ou nada

O ativo de midia caminha por quatro estados: `registered` → `ingested` → `analyzed`, com
`failed` para o caso terminal.

`POST /media/{id}/ingest` executa **a ingestao possivel**: sondagem com FFmpeg, extracao
de audio, ASR, deteccao de cenas e OCR. Cada etapa cujo adaptador nao esteja instalado e
**registrada e pulada** — nenhuma indisponibilidade, e nenhuma falha de adaptador, derruba
a ingestao. O relatorio devolve o que foi alcancado, e o `status` do ativo reflete
exatamente isso.

E por isso que `can_ingest` em `/capabilities` e `probe AND asr`: sem FFmpeg e sem
WhisperX nao ha o que extrair de um arquivo de video. Mas `can_detect` continua `true` —
porque a transcricao pode ter vindo pela importacao, e o funil so precisa dela.

### 10.4 Como cada sinal e calculado

Tudo em `domain/services/matching.py` — dominio puro, sem I/O, sem `numpy`.

| Sinal | Peso | Calculo |
| --- | --- | --- |
| **lexico** (`speech_match`) | 0.40 | melhor entre `token_set_ratio`, `token_sort_ratio` e `partial_ratio` do `rapidfuzz` sobre os textos normalizados; sem `rapidfuzz`, `difflib.SequenceMatcher` combinado com Jaccard de tokens |
| **semantico** (`semantic_match`) | 0.25 | cosseno entre o vetor da janela e o do fingerprint, **reescalado** de `[-1, 1]` para `[0, 1]` |
| **ocr** (`ocr_match`) | 0.15 | melhor entre a similaridade do texto em tela com o texto do comercial e a melhor similaridade com qualquer palavra-chave |
| **visual** (`visual_match`) | 0.15 | veredito do juiz multimodal; **na ausencia dele, herda o sinal de fala como proxy conservador** |
| **duracao** (`duration_match`) | 0.05 | `1 - min(1, |dur_janela - duracao_esperada| / max(duracao_esperada, 1))` |

Duas salvaguardas contra o auto-engano:

- **a heranca visual viaja na evidencia.** Quando nao houve juiz, `visual_from_proxy=True`
  acompanha a deteccao. Sem isso, um `0.91` herdado ficaria indistinguivel de um `0.91`
  que o modelo realmente afirmou. O mesmo vale para `ocr_available`.
- **a ordem temporal penaliza, nao inventa.** `OrderMatcher` reduz as ancoras
  (`key_phrases`, senao `keywords`) a tokens unicos na ordem esperada, mede quanto a
  sequencia observada e crescente (LCS sobre as ancoras encontradas) e, abaixo de 0.7,
  multiplica o score final por 0.85. Sem ancoras, ou sem nenhuma ancora presente, a ordem
  **nada afirma**: `(1.0, True)`.

Os pesos sao configuraveis, e a soma **precisa** valer 1.0 — a validacao recusa a
configuracao com tolerancia de 1e-6, em vez de normalizar em silencio.

### 10.5 As tres faixas de decisao

| Faixa | Status | O que acontece |
| --- | --- | --- |
| `S ≥ 0.90` | `accepted` | aceita sem juiz multimodal |
| `0.60 ≤ S < 0.90` | `needs_review` | juiz Qwen-VL decide; sem ele, vai para a fila de revisao humana |
| `S < 0.60` | `rejected` | descartado (persistido apenas com `--keep-rejected`) |

### 10.6 O teto que impede a afirmacao sem prova

`GET /api/v1/adwatch/capabilities` devolve, alem do que esta instalado:

```jsonc
{
  "capabilities": { "probe": false, "asr": false, "ocr": false, "scenes": false, "vision": true },
  "can_ingest": false, "can_detect": true,
  "weights":    { "lexical": 0.4, "semantic": 0.25, "ocr": 0.15, "visual": 0.15, "duration": 0.05 },
  "thresholds": { "accept": 0.9, "review": 0.6 },
  "max_score_without":  { "ocr": 0.85, "vision": 0.85 },
  "max_score_effective": 0.85
}
```

`max_score_effective` e a leitura mais importante do endpoint, e a distincao entre ele e
`max_score_without` e deliberada:

- `max_score_without` responde a duas perguntas **hipoteticas** — quanto se perde sem OCR
  (`1 - 0.15 = 0.85`), quanto se perderia sem juiz visual;
- `max_score_effective` responde a pergunta que o operador realmente tem: **com esta
  maquina, do jeito que ela esta, ate onde uma deteccao consegue chegar?**

E so o OCR entra nessa conta. A ausencia do juiz visual **nao** subtrai peso, porque
`visual_match` herda `speech_match` como proxy; a ausencia do OCR subtrai, porque nao ha
de onde herdar texto em tela.

O numero decide o funil inteiro: **sem OCR o teto e 0.85, abaixo do limiar de aceite de
0.90 — nenhuma deteccao e aceita automaticamente, todas caem em revisao humana.** Medido
em producao: em 81 deteccoes reais, a maior confianca foi 0.845 e nenhuma passou de 0.90.

Nao e um acaso aritmetico, e a consequencia desejada. Uma instalacao sem OCR **nao
consegue** afirmar sozinha; ela para em `needs_review` e pede um humano. O sistema
prefere admitir incerteza a afirmar sem evidencia — e o console diz isso na tela, com o
teto em porcentagem, em vez de deixar o operador descobrir pela fila de revisao que nunca
esvazia.

### 10.7 NMS e refino de fronteira

- **NMS (`NonMaximumSuppression`)**: candidatos do mesmo comercial com IoU temporal acima
  de 0.5 sao fundidos. Mantem-se o de maior score e o intervalo e expandido para a uniao
  dos absorvidos — resolvendo a sobreposicao natural das janelas de 15/30/60 s.
- **Refino (`BoundaryRefiner`)**: as bordas sao encaixadas no corte de cena mais proximo,
  ate 3 s de deslocamento. Empate de distancia fica com a fronteira anterior. Se o
  encaixe inverteria o intervalo, o refino e descartado.

### 10.8 O caminho offline e a prova

O caminho de **importacao de transcricao** (JSON no formato WhisperX) torna o funil
inteiro executavel sem FFmpeg, sem GPU e sem rede. E o caminho usado nos testes e em
`scripts/prova_adwatch.py`, que imprime a decomposicao do score parcela por parcela:

```
=== 3. deteccao ===
  COM_000234    122.3-150.0   conf=0.777  needs_review fala=0.91 sem=0.90 ordem=sim cena=nao
=== 4. com cortes de cena (refino de fronteira) ===
  COM_000234    119.5-150.5   conf=0.777  needs_review fala=0.91 sem=0.90 ordem=sim cena=sim
=== 5. veredito ===
  comercial presente vira candidato........... OK
  bordas encaixadas nos cortes de cena........ OK
  classificado needs_review (sem OCR/VLM)..... OK
  comercial fora de ordem descartado.......... OK
  comercial ausente do audio descartado....... OK
  erro de fronteira apos refino .............. 0.00s

  Decomposicao do score 0.777:
    fala x0.40      = 0.91 -> 0.365
    semantico x0.25 = 0.90 -> 0.225
    ocr x0.15       = 0.00 -> 0.000   (sem OCR offline: e isto que segura abaixo de 0.90)
    visual x0.15    = 0.91 -> 0.137
    duracao x0.05   = 1.00 -> 0.050
```

O comercial presente para em `needs_review` porque falta OCR: e o pipeline obedecendo a
SPEC-0010 secao 3.6, nao um defeito.

### 10.9 Por que o funil e barato

O argumento economico da inversao (secao 10.1) e estrutural, mas da para medir. Na
execucao de demonstracao — 300 s de midia, catalogo de 5 comerciais, modo offline:

```
janelas=172  candidatos=99  comerciais=5  persistidas=1  tempo=95ms
```

172 janelas avaliadas contra 5 fingerprints em **95 ms de CPU**, sem uma unica chamada de
GPU no caminho textual. O modelo multimodal so entraria na faixa 0.60–0.90 — e mesmo la
existe um **teto defensivo de 24 chamadas por execucao** (`MAX_VISION_CALLS`).

O teto tem um motivo preciso: a classificacao acontece **antes** da supressao, entao uma
midia longa pode produzir centenas de candidatos na faixa de revisao, e sem teto uma unica
deteccao dispararia centenas de chamadas caras. Os candidatos sao ordenados por score
decrescente e os excedentes **permanecem em revisao humana** em vez de serem descartados —
o corte economiza chamada, nao evidencia.

Esse e o padrao do projeto inteiro: o caminho caro e o ultimo, e ele e limitado.

### 10.10 O que fica registrado

Cada `Detection` guarda `start`, `end`, `confidence`, `status`, `refined_by_scene`,
`verified_by_vlm` e a `DetectionEvidence` completa — os cinco sinais, `order_ok`,
`brand_detected`, `matched_text`, `visual_from_proxy` e `ocr_available`. Um revisor
consegue reconstruir **por que** aquele numero saiu, sinal por sinal. E o `PATCH` de
revisao humana grava a decisao sem apagar a evidencia original.

Detalhes normativos: [`specs/0010-adwatch.spec.md`](specs/0010-adwatch.spec.md) e
[`docs/adr/0004-retrieval-antes-do-vlm.md`](docs/adr/0004-retrieval-antes-do-vlm.md).

---

## 11. FinOps

Cada invocacao gera um `UsageRecord` com tokens de entrada e saida, modelo, modulo,
tenant e custo calculado a partir da tabela de precos (`input_usd_per_1k`,
`output_usd_per_1k`). O custo agregado no run tem 8 casas decimais — chamadas baratas
nao somam zero por arredondamento.

Modelos **sem preco cadastrado** nao somem: caem no preco padrao e aparecem em
`unknown_models` no resumo, para que ninguem descubra depois que 30% do consumo estava
fora da conta.

Orcamentos tem escopo em string (`global`, `module:<slug>`, `tenant:<id>`), periodo
(`daily`, `weekly`, `monthly`, `total`), `alert_threshold` (padrao 0.8) e `hard_stop`.
Com `hard_stop: true`, o estouro recusa a execucao na **etapa 4** de `InvokeModule` —
antes do guardrail, antes do prompt, antes do provedor.

Consultas: `/api/v1/finops/summary`, `/series`, `/usage`, `/budgets/{id}/status`.

---

## 12. Identidade e acesso

Quatro papeis e treze permissoes granulares:

| Papel | Permissoes |
| --- | --- |
| `root` | todas, inclusive `admin:*` |
| `admin` | todas, inclusive `admin:*` |
| `operator` | todas as de leitura + `module:invoke` + `knowledge:write` |
| `viewer` | apenas as de leitura (`*:read`) |

Permissoes: `module:read` · `module:write` · `module:invoke` · `prompt:read` ·
`prompt:write` · `guardrail:read` · `guardrail:write` · `knowledge:read` ·
`knowledge:write` · `finops:read` · `finops:write` · `run:read` · `admin:*`.

Dois esquemas de credencial convivem, ambos declarados no OpenAPI:

**`Authorization: Bearer <JWT>`** — HS256, assinado com `LUKATO_SECURITY__JWT_SECRET`,
expiracao de 3600 s por padrao, renovavel por `POST /identity/token/refresh`. Claims:
`sub`, `role`, `tenant`, `kind`, `iat`, `exp` e `iss="lukato"`.

O token **nao carrega a lista de permissoes**, e a decisao e deliberada: o `Principal` e
reconstruido no `decode` sempre a partir de `ROLE_PERMISSIONS[role]`. Duas consequencias
que so aparecem por causa disso — mudar o mapa de permissoes de um papel vale
**imediatamente** para os tokens ja emitidos (nao ha janela ate expirar), e um token
adulterado nao consegue pedir permissao que o seu papel nao tem, porque a permissao nunca
esteve escrita nele. Qualquer falha de validacao — assinatura, expiracao, emissor, papel
desconhecido — vira `UnauthorizedError`; nenhuma excecao da biblioteca de JWT vaza para a
camada HTTP.

**`X-API-Key: lk_<prefixo>_<segredo>`** — o prefixo (8 caracteres) indexa a linha no
banco e o segredo (32 bytes, `secrets.token_urlsafe`) e conferido contra o
`hashed_secret`. O banco guarda **apenas o prefixo e o hash**; a chave completa e exibida
uma unica vez, na criacao. Chaves tem papel, tenant, validade opcional e registro de
ultimo uso. Rotacao e revogacao sao endpoints proprios.

**Senhas**: bcrypt com custo 12, sem `passlib`. Ha uma sutileza tratada explicitamente —
o bcrypt trunca em silencio qualquer entrada acima de 72 bytes, o que faria duas senhas
longas com o mesmo prefixo virarem a mesma credencial. A senha e reduzida a 64 bytes ASCII
por SHA-256 **antes** do bcrypt, entao o comprimento inteiro conta.

`LUKATO_SECURITY__AUTH_ENABLED=false` (padrao de desenvolvimento) faz a aplicacao operar
com um principal implicito — comodo para desenvolver, **inaceitavel em producao**. A
etapa 3 de `InvokeModule` exige `MODULE_INVOKE` de qualquer forma: `prova_trinca.py`
asercao 6 confirma que um `viewer` recebe `403`.

---

## 13. Observabilidade

**Metricas Prometheus** em `/metrics`:

```
lukato_http_requests_total              por metodo, template de rota e status
lukato_http_request_duration_seconds    histograma de latencia HTTP
lukato_module_invocations_total         por modulo e status final
lukato_module_latency_seconds           latencia ponta a ponta da invocacao
lukato_llm_tokens_total                 por modelo e tipo (prompt/completion)
lukato_llm_cost_usd_total               custo acumulado por modelo e modulo
lukato_guardrail_findings_total         por estagio, tipo de regra e acao aplicada
lukato_guardrail_blocks_total           bloqueios efetivos por estagio e politica
lukato_provider_errors_total            erros de provedores externos, por codigo
```

O par `guardrail_findings_total` / `guardrail_blocks_total` responde, sem consulta ao
banco, a pergunta que auditoria faz: quanto a plataforma barrou, onde e por qual regra.

**Log estruturado** (`structlog`), com `LUKATO_OBSERVABILITY__LOG_JSON=true` para
ingestao. **Tracing** opcional no Langfuse: cada run abre um span, com geracoes aninhadas
por chamada de LLM. Sem credencial, o `NoopTracer` assume, `/readyz` reporta `degraded`
para o componente `tracer` e nada mais muda.

O `trace_id` viaja no `AgentRun` e na resposta da API, e o `request_id` e propagado por
middleware — o mesmo identificador liga log, metrica, trace e registro no banco.

---

## 14. Modo degradado

| Componente | Modo normal | Modo degradado | Escolha |
| --- | --- | --- | --- |
| LLM | `OpenAICompatibleLLM` (hub) | `EchoLLM` | **automatica** ao faltar credencial |
| Banco | PostgreSQL 16 + pgvector | SQLite + cosseno em memoria | **automatica** com `AUTO_FALLBACK=true` |
| Tracer | Langfuse | `NoopTracer` | **automatica** sem credencial |
| Busca vetorial | HNSW `vector_cosine_ops` | cosseno em `numpy` | segue o banco |
| Embeddings | `Qwen3-Embedding-0.6B` | `HashingEmbedder` | **explicita** (secao 9.3) |
| Probe/ASR/OCR/Cenas | FFmpeg · WhisperX · PaddleOCR · PySceneDetect | importacao de JSON | por disponibilidade |

O modo degradado **nunca se disfarca de modo normal**. Quem consome sabe em que modo
esta: `/readyz`, `/api/v1/adwatch/capabilities` e o console dizem, e o log registra o
motivo em texto. Exemplo real de `/readyz` em modo offline:

```jsonc
{
  "status": "degraded",
  "components": {
    "database":   { "status": "ok",       "detail": "4 definicao(oes) de modulo" },
    "registry":   { "status": "ok",       "detail": "5 modulo(s) registrado(s)" },
    "llm":        { "status": "ok",       "detail": "modelo 'echo'" },
    "embeddings": { "status": "ok",       "detail": "modelo 'hashing-local' com 1024 dimensoes" },
    "tracer":     { "status": "degraded", "detail": "tracer no-op: traces nao sao enviados" }
  },
  "version": "1.0.0", "environment": "dev"
}
```

E `capabilities` do AdWatch nao apenas lista o que falta: entrega a **dica de instalacao**
de cada capacidade ausente ("instale o FFmpeg... e garanta que `ffmpeg` e `ffprobe`
estejam no PATH").

---

## 15. Configuracao

Tudo por variavel de ambiente, prefixo `LUKATO_`, aninhamento com `__`:

```
LUKATO_LLM__MODEL=qwen-latest   →   settings.llm.model
```

Nove grupos. Os valores abaixo sao os **defaults do codigo**; a lista completa e
comentada esta em [`.env.example`](.env.example).

| Grupo | Variaveis principais (default) |
| --- | --- |
| `APP` | `NAME=lukato` · `ENV=dev` · `DEBUG=false` · `HOST=0.0.0.0` · `PORT=8000` · `ROOT_PATH=` · `WORKERS=1` · `DOCS_ASSETS_BASE=https://cdn.jsdelivr.net/npm` |
| `DB` | `URL=postgresql+asyncpg://lukato:lukato@localhost:5432/lukato` · `FALLBACK_URL=sqlite+aiosqlite:///./lukato.db` · `AUTO_FALLBACK=true` · `CREATE_ALL=true` · `POOL_SIZE=10` · `MAX_OVERFLOW=20` |
| `LLM` | `PROVIDER=openai_compatible` (`\|echo`) · `BASE_URL=https://hub-gpus.usto.re/v1` · `API_KEY=` **(segredo)** · `MODEL=qwen-latest` · `FALLBACK_MODEL=openai/gpt-oss-20b` · `TEMPERATURE=0.2` · `MAX_TOKENS=2048` · `TIMEOUT=60` · `MAX_RETRIES=3` |
| `EMBEDDING` | `PROVIDER=qwen` (`\|hashing`) · `BASE_URL=https://hub-gpus.claro.com.br/embed06b/v1` · `MODEL=Qwen/Qwen3-Embedding-0.6B` · `DIMENSIONS=1024` · `BATCH_SIZE=32` · `COLLECTION=agente_evidence` |
| `GUARDRAILS` | `ENABLED=true` · `FAIL_OPEN=false` · `REDACTION_TOKEN=[REDIGIDO]` · `MAX_INPUT_CHARS=32000` · `MAX_OUTPUT_CHARS=32000` |
| `OBSERVABILITY` | `LANGFUSE_ENABLED=false` · `LANGFUSE_HOST=https://cloud.langfuse.com` · chaves **(segredo)** · `LOG_LEVEL=INFO` · `LOG_JSON=false` · `METRICS_ENABLED=true` |
| `SECURITY` | `AUTH_ENABLED=false` · `JWT_SECRET` **(segredo)** · `JWT_ALGORITHM=HS256` · `JWT_EXPIRES_SECONDS=3600` · `API_KEY_HEADER=X-API-Key` · `CORS_ORIGINS=["*"]` |
| `FINOPS` | `ENABLED=true` · `CURRENCY=USD` · `DEFAULT_INPUT_USD_PER_1K=0.0` · `DEFAULT_OUTPUT_USD_PER_1K=0.0` |
| `ADWATCH` | `WINDOW_SIZES=[15.0,30.0,60.0]` · `WINDOW_STRIDE=5.0` · pesos `0.40/0.25/0.15/0.15/0.05` · `ACCEPT_THRESHOLD=0.90` · `REVIEW_THRESHOLD=0.60` · `TOP_K_RETRIEVAL=10` · `TOP_K_RERANK=3` · `WORKDIR=./var/adwatch` · `UPLOAD_MAX_MB=2048` |

Validacoes que **recusam** em vez de corrigir em silencio:

- os cinco pesos do AdWatch precisam somar 1.0 (tolerancia 1e-6);
- `REVIEW_THRESHOLD` nao pode ser maior que `ACCEPT_THRESHOLD`;
- `EMBEDDING__PROVIDER` aceita apenas `qwen` ou `hashing`;
- a dimensao de embedding gravada na colecao precisa bater com a configurada.

> **Segredos.** A chave do hub GPU, o segredo JWT e as credenciais Langfuse vao para
> cofre corporativo (Vault, AWS Secrets Manager, ExternalSecrets) — nunca para o
> repositorio nem para a imagem. O `.env` esta no `.gitignore`; em Kubernetes os segredos
> chegam por `secretKeyRef`. O console mascara segredos na tela de configuracoes.

---

## 16. Persistencia e migracoes

**18 tabelas**, uma unica trilha de migracao Alembic (`0001_esquema_inicial`,
`0002_indices_pgvector`):

```
modules · prompts · guardrail_policies · agent_runs · run_steps
usage_records · budgets · documents · chunks · users · api_keys
commercials · ad_fingerprints · media_assets · transcripts · scene_cuts
ocr_texts · detections
```

```bash
make migrate                       # alembic upgrade head
make migration m="mensagem"        # revisao autogerada
make downgrade                     # volta uma migracao
```

### O problema que o ADR-0006 resolve

O alvo de producao e PostgreSQL; dev e CI rodam em SQLite. O Alembic autogera contra o
dialeto conectado, e gerar em SQLite produzia dois defeitos **silenciosos**: `VectorType`
era escrito sem o import correspondente (`NameError` na primeira execucao), e `JSONType`
era expandido como `postgresql.JSONB(astext_type=Text())`, tambem sem import. Os dois
passariam em revisao — o arquivo *parece* correto.

A solucao: um `render_item` em `migrations/env.py` que emite os tipos proprios do projeto
e registra os imports. O que e genuinamente especifico do PostgreSQL — a extensao
`vector`, os indices HNSW, os indices `gin_trgm` — fica isolado em `0002`, que verifica o
dialeto e vira no-op nos demais.

O resultado e verificado por teste: `alembic upgrade head` e `Base.metadata.create_all`
sao comparados tabela a tabela e coluna a coluna. **Uma trilha, um `head`, o mesmo schema
testado e implantado.**

---

## 17. Implantacao

### 17.1 As quatro listas de dependencia

| Arquivo | Para que serve |
| --- | --- |
| `requirements.txt` | runtime. FastAPI 0.141, Pydantic 2.13, SQLAlchemy 2.0, LangGraph 1.2, `deepagents` 0.7, `openai` 3.3, Langfuse 4.14, structlog, `prometheus-client`, `rapidfuzz`, `numpy`, `bcrypt` — todos pinados |
| `requirements-dev.txt` | qualidade: `pytest` 9.1, `pytest-asyncio`, `pytest-cov`, `ruff` 0.16, `mypy` 2.3 |
| `requirements-media.txt` | pipeline multimodal **opcional**: `ffmpeg-python`, `scenedetect[opencv]`, `whisperx` 3.8.6, `paddleocr` 3.4, `faiss-cpu` |
| `requirements-media-image.txt` | subset CPU para a imagem Docker (`WITH_MEDIA=1`), sem OCR nem faiss |

A separacao nao e cosmetica. O modulo AdWatch **funciona sem** a terceira lista: os
adaptadores de midia detectam capacidade em tempo de execucao e degradam para os
importadores JSON. Instalar `requirements-media.txt` e uma escolha para quem vai
processar arquivos de video de verdade — nao um pre-requisito para usar a plataforma.

### 17.2 A imagem

Docker multi-stage. O `Dockerfile` **nao roda `apt-get`**, entao o build nao depende de
alcancar um espelho Debian. O que ele ainda precisa alcancar:

1. o registry da imagem base — trocavel por build-arg:
   `make docker-build-mirror PYTHON_IMAGE=mirror.gcr.io/library/python:3.11-slim-bookworm`
2. o PyPI, para os wheels (todos binarios: nada compila no build);
3. o binario estatico do `tini`, conferido por sha256 —
   `--build-arg TINI_URL=<url> --build-arg TINI_SHA256=<sha>`.

Proxy com interceptacao TLS: ponha a CA em `deploy/ca/*.crt`; o builder acrescenta ao
conjunto de confianca antes de baixar qualquer coisa.

A imagem final: usuario nao-root (uid 10001), filesystem raiz somente leitura, `tini`
como PID 1, `HEALTHCHECK` em `/healthz` feito com o **proprio Python** (nao ha `curl` na
imagem), sem `libpq5` (o `asyncpg` fala o protocolo do PostgreSQL direto) e sem
compilador. O entrypoint aceita `serve` (padrao), `migrate`, `seed` e `shell`.

**Capacidade multimodal opt-in:** `docker build --build-arg WITH_MEDIA=1 .` instala
FFmpeg/FFprobe estaticos, conferidos por sha256 no mesmo padrao do `tini`. Sem esse
build-arg, a imagem avisa explicitamente que nao ha `ffmpeg` e como reconstruir.

### 17.3 Escala

O adjetivo "escalavel" do titulo tem tres significados distintos neste projeto, e vale
separar:

**Escala de funcionalidade.** O custo marginal de um agente novo e uma linha no banco.
Nao ha repositorio, pipeline nem deploy por agente. Dez agentes e uma instalacao; cem
agentes e a mesma instalacao com cem linhas.

**Escala de carga.** A aplicacao e sem estado — sessao nenhuma vive no processo, o estado
vive no PostgreSQL. Isso permite replicas horizontais: o HPA v2 sobe de **2 para ate 10
replicas** por CPU (70%) e memoria (80%), com janela de estabilizacao de 30 s para subir e
300 s para descer, dobrando a capacidade a cada 30 s quando precisa e removendo um pod por
minuto quando sobra. Cada pod pede 250m de CPU e 512Mi, com teto de 1 CPU e 1Gi.
`topologySpreadConstraints` espalha as replicas, o `PodDisruptionBudget` protege durante
manutencao e o `preStop` drena as conexoes antes do encerramento.

**Escala de dado.** Embeddings vao em lote (32 por chamada), a busca usa indice HNSW no
pgvector, e o funil do AdWatch e barato por construcao (secao 10.9). Quando o catalogo de
comerciais crescer alem do que o pgvector atende bem, a troca ja esta prevista: a porta
`VectorStorePort` isola a decisao, e `faiss-cpu` esta em `requirements-media.txt`
justamente para isso (ADR-0005).

### 17.4 Kubernetes

`deploy/k8s/` com Kustomize (base + overlays `dev`, `prod`, `oke`):

Namespace · ServiceAccount · ConfigMap · Deployment (probes, recursos, securityContext
restrito, topology spread, preStop) · Service · HPA v2 · PodDisruptionBudget · Ingress ·
NetworkPolicy · Job de migracao (hook PreSync do ArgoCD) · ServiceMonitor.

`deploy/k8s/base/secret.example.yaml` contem **apenas placeholders** e nao entra no
`kustomization`. Em producao use ExternalSecrets/Vault. Nenhum segredo real e versionado
— e o CI verifica isso.

### 17.5 CI

Quatro jobs em `.github/workflows/ci.yml`:

1. **lint · tipos · testes** — `ruff check`, `ruff format --check`, `mypy src/lukato`,
   `pytest` com cobertura, e exportacao do contrato OpenAPI;
2. **integracao com PostgreSQL + pgvector** — servico real, `alembic upgrade head`,
   `pytest -m integration`;
3. **build da imagem** — constroi, sobe o container e checa `/healthz`;
4. **validacao dos manifestos Kubernetes** — `kustomize build` de cada overlay, contagem
   de recursos e verificacao de que nenhum segredo real foi versionado.

---

## 18. Qualidade e provas executaveis

```bash
make lint     # ruff check
make fmt      # ruff format + fix
make type     # mypy (estrito em domain/ e application/)
make test     # pytest — roda offline, sem PostgreSQL e sem rede
make cov      # cobertura
make check    # lint + type + test
```

Marcadores: `unit` (puros, sem I/O), `integration` (sobem a aplicacao ou o banco),
`contract` (contrato OpenAPI), `slow`.

A suite tem **1.140 testes em 39 arquivos** entre unidade, integracao e contrato, e passa
inteira offline — sem PostgreSQL, sem GPU e sem rede. `EchoLLM`, `HashingEmbedder`,
`NoopTracer`, SQLite e os importadores JSON de transcricao, cenas e OCR substituem tudo
que exigiria a rede corporativa.

Dois testes merecem destaque:

- **`tests/unit/test_architecture.py`** falha se a regra hexagonal for violada — se
  `domain/` passar a importar `sqlalchemy`, `fastapi`, `httpx`, `openai`, `langgraph`,
  `langfuse` ou `jinja2`. Inclui imports feitos dentro de funcoes.
- **`tests/contract/test_openapi.py`** verifica o contrato: que o documento servido e
  igual ao exportado, que e OpenAPI 3.1.0, que **todo** `operationId` existe e e unico,
  que os caminhos normativos das specs estao publicados (inclusive `/healthz`, `/readyz`
  e `/metrics`), que toda operacao declara ao menos uma tag e que nenhuma usa tag fora
  do catalogo de dez, e que os esquemas `bearerAuth` e `apiKeyAuth` estao declarados.

### Provas executaveis

Para quando ler o codigo nao basta. Cada uma monta o proprio banco descartavel e nao toca
no seu: rode quantas vezes quiser, com ou sem `.env`.

```bash
python scripts/prova_trinca.py     # o requisito central, em 7 asercoes (secao 2.3)
python scripts/prova_adwatch.py    # o funil do AdWatch sem FFmpeg/GPU/rede (secao 10.8)
python scripts/navegacao_fim_a_fim.py  # as 30 operacoes de escrita do console, clicando
```

As duas primeiras nao exigem nada: montam o proprio banco descartavel e nao tocam no seu.
A terceira exige a aplicacao no ar em `http://127.0.0.1:8000` e o Chromium do Playwright,
e existe por um motivo especifico: **a bateria de testes nao clica**. Cinco defeitos so
apareceram quando alguem clicou — entre eles, o ouvinte do painel de contexto engolindo o
clique de qualquer botao dentro de uma `<tr>`, o que deixava *todas* as acoes de linha
mudas, sem erro nenhum. Cada operacao do script confere o **estado depois do clique**,
lendo a API: a versao anterior assertava so que a URL nao tinha caido no `/api/`, e dava
verde em cinco operacoes que nunca gravaram nada. Cada rodada usa o proprio sufixo, entao
rodar de novo mede convivencia com o que a rodada anterior deixou.

`fixtures/demo-export.json` e um `lukato export` completo de uma instalacao de
demonstracao — util para levantar um ambiente com dados realistas em um comando.

---

## 19. Estrutura de diretorios

```
specs/          especificacoes normativas (SDD) — o codigo obedece a elas
docs/           arquitetura, ADRs, notas de biblioteca, guia de deploy
fixtures/       export de demonstracao
scripts/        entrypoint do container, provas executaveis, navegacao fim a fim
src/lukato/
  domain/       nucleo puro: modelos, portas, servicos (zero I/O)
    models/       module · prompt · guardrail · run · finops · identity · knowledge · adwatch
    ports/        llm · embeddings · vector_store · orchestrator · guardrail ·
                  observability · media · repositories · unit_of_work
    services/     guardrail_engine · module_composer · cost_calculator ·
                  matching · text_normalizer · transcript_search
  application/  casos de uso (10 modulos) + container + DTOs
  adapters/     driven: persistence · llm · embeddings · guardrails ·
                orchestrator · observability · media
  interfaces/   driving: HTTP (API v1 + console_forms) · UI (Jinja2) · CLI
  modules/      building blocks + registry
  config/       settings (9 grupos) e logging
  composition.py  composition root — o unico lugar que enxerga tudo
migrations/     Alembic (uma trilha, dois arquivos)
deploy/k8s/     Kustomize (base + overlays dev/prod/oke)
tests/          unit · integration · contract
```

---

## 20. Documentacao normativa

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
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | guia de implantacao |
| [`docs/LIBRARY-NOTES.md`](docs/LIBRARY-NOTES.md) | APIs reais das versoes usadas |
| [`readme.txt`](readme.txt) | guia operacional em texto puro |

### Decisoes de arquitetura (ADRs)

| ADR | Decisao | Por que importa |
| --- | --- | --- |
| [0001](docs/adr/0001-arquitetura-hexagonal.md) | arquitetura hexagonal | trocar provedor e escrever um adaptador; a regra e verificavel por teste |
| [0002](docs/adr/0002-trinca-parametrizavel.md) | trinca como invariante | garantia **estrutural**, nao documental |
| [0003](docs/adr/0003-degradacao-offline.md) | degradacao offline explicita | a suite roda offline; o degradado nunca se disfarca de normal |
| [0004](docs/adr/0004-retrieval-antes-do-vlm.md) | retrieval antes do modelo multimodal | custo cai por ordens de grandeza, com evidencia auditavel |
| [0005](docs/adr/0005-postgres-pgvector.md) | PostgreSQL + pgvector como unico armazenamento | uma transacao cobre dados e vetores; um backup; um servico |
| [0006](docs/adr/0006-migracoes-portateis.md) | migracoes portateis PG/SQLite | o schema testado em CI e o mesmo implantado |

---

## 21. Solucao de problemas

| Sintoma | Causa | O que fazer |
| --- | --- | --- |
| As respostas ecoam a pergunta | o adaptador caiu para `EchoLLM` | confira `GET /readyz` → `components.llm`; preencha `LUKATO_LLM__API_KEY` |
| `provider_error` ao invocar, mesmo sem chave | o comentario na linha da chave virou o valor (secao 4.2) | apague o comentario da linha, ou fixe `LUKATO_LLM__PROVIDER=echo` |
| A busca semantica devolve resultados ruins | provedor de embeddings em modo `hashing` | confira `GET /readyz` → `components.embeddings` |
| Erro de dimensao ao gravar embeddings | `DIMENSIONS` diverge do que a colecao registrou | reindexe a colecao ou volte a dimensao anterior |
| Timeout ou 000 ao chamar o hub | `hub-gpus.usto.re` e `hub-gpus.claro.com.br` sao hosts internos | fora da rede corporativa, use o modo offline |
| `429` do provedor | limite de requisicoes | o adaptador ja faz retry com backoff; reduza a concorrencia ou peca cota |
| PostgreSQL indisponivel no boot | fallback automatico | com `AUTO_FALLBACK=true` cai para SQLite e loga WARNING; em producao use `false` |
| `/api/docs` responde 200 em branco | o navegador nao alcanca o CDN | aponte `LUKATO_APP__DOCS_ASSETS_BASE` para o espelho interno |
| AdWatch nunca aceita automaticamente | sem OCR o teto de score e 0.85 (secao 10.6) | instale o OCR, ou revise manualmente a fila `needs_review` |
| `CERTIFICATE_VERIFY_FAILED` no build | proxy com interceptacao TLS | ponha a CA em `deploy/ca/*.crt` |

---

## 22. Seguranca

Checklist antes de ir para producao:

```
[ ] LUKATO_SECURITY__AUTH_ENABLED=true
[ ] LUKATO_SECURITY__JWT_SECRET forte (openssl rand -hex 32), vindo do cofre
[ ] LUKATO_SECURITY__CORS_ORIGINS restrito (nunca ["*"])
[ ] LUKATO_APP__DEBUG=false e LUKATO_APP__ENV=prod
[ ] LUKATO_DB__AUTO_FALLBACK=false (falhar rapido, nao degradar em silencio)
[ ] LUKATO_GUARDRAILS__FAIL_OPEN=false
[ ] chaves de LLM/embeddings/Langfuse via Secret do Kubernetes ou Vault
[ ] .env fora do controle de versao (ja esta no .gitignore)
[ ] guardrails de entrada e saida vinculados a TODOS os modulos ativos
[ ] orcamentos FinOps com hard_stop nos modulos expostos ao publico
[ ] senha do root trocada no primeiro acesso
```

Politica de divulgacao de vulnerabilidades: [`SECURITY.md`](SECURITY.md).

---

<sub>lukato 1.0.0 · Sergio Felipe Bezerra Gaiotto · licenca proprietaria.
As especificacoes em `specs/` sao normativas: quando este README e uma spec divergirem, a
spec vence.</sub>
