# Arquitetura do lukato

> Documento de visao. As regras **normativas** estao em `specs/`; este texto explica
> as decisoes e como as pecas se encaixam.

## 1. Por que hexagonal

O nucleo do lukato precisa sobreviver a trocas que **vao** acontecer: outro provedor de
LLM, outro banco vetorial, outro backend de tracing, outro runtime de agente. A
arquitetura hexagonal (Ports & Adapters) coloca essas decisoes na borda.

```text
                       ┌───────────────────────────────────┐
   driving adapters    │            APPLICATION            │    driven adapters
   (quem chama)        │        (casos de uso)             │    (quem e chamado)
                       │   ┌───────────────────────────┐   │
  HTTP  ──────────────▶│   │          DOMAIN           │   │◀────────── PostgreSQL
  UI (Jinja2) ────────▶│   │  modelos · portas ·       │   │◀────────── pgvector
  CLI  ──────────────▶ │   │  servicos puros           │   │◀────────── Qwen (LLM)
  modules ────────────▶│   └───────────────────────────┘   │◀────────── Qwen (embeddings)
                       └───────────────────────────────────┘◀────────── Langfuse
                                                             ◀────────── FFmpeg/WhisperX/OCR
```

**Regra de dependencia** (verificada por `tests/unit/test_architecture.py`):
as setas apontam sempre para dentro. `domain/` nao conhece `sqlalchemy`, `fastapi`,
`httpx`, `openai`, `langgraph`, `langfuse` nem `jinja2`. O unico lugar que enxerga
tudo ao mesmo tempo e o *composition root* (`lukato/composition.py`).

O ganho concreto: o motor de guardrails, o calculo de custo e o motor de matching do
AdWatch sao testaveis sem banco, sem rede e sem mock de biblioteca — sao funcoes puras
sobre modelos puros.

## 2. As tres camadas de execucao

| Camada | Pergunta que responde | Exemplo |
| --- | --- | --- |
| `domain` | *quais sao as regras?* | "um bloqueio de guardrail interrompe a cadeia" |
| `application` | *qual e o passo a passo?* | `InvokeModule`: guardrail → prompt → runtime → guardrail → custo → run |
| `adapters` | *como falo com o mundo?* | `OpenAICompatibleLLM`, `SqlAlchemyRunRepository`, `LangfuseTracer` |

## 3. Building blocks

Um modulo e uma **classe** (`BaseModule`) mais uma **definicao** (`ModuleDefinition`
persistida). A classe traz comportamento; a definicao traz configuracao — binding,
modelo, ferramentas, status. Uma classe pode ter varias definicoes.

Isso e o que torna `processing` interessante: ele nao tem regra de negocio. Duas
definicoes sobre a mesma classe, com system prompts e guardrails diferentes, sao dois
agentes diferentes. Criar um agente vira uma operacao de CRUD.

Descoberta: `registry.load_builtin()` para os embutidos e
`registry.discover("lukato.modules")` para entry points de terceiros. Um pacote externo
instalado no ambiente aparece no registry e no menu da UI sem tocar no nucleo.

## 4. A trinca parametrizavel

Requisito central do projeto, tratado como invariante do sistema:

```text
guardrail de entrada  →  system prompt  →  guardrail de saida
```

`InvokeModule` e o **unico** caminho de execucao, e a ordem das 11 etapas
(SPEC-0001 secao 4) nao admite atalho. Consequencias praticas:

* Um bloqueio na entrada acontece **antes** de qualquer byte sair para o provedor.
* Toda invocacao vira um `AgentRun` persistido — nao existe execucao invisivel.
* Trocar a politica de um modulo em producao e um `PUT`, sem redeploy.
* Custo e tokens sao capturados no mesmo lugar, sempre.

## 5. Degradacao como requisito, nao como acidente

Os hosts do hub GPU sao internos. Em vez de tratar "sem rede" como erro, o sistema
trata como **modo de operacao**:

| Componente | Preferencial | Degradado | Sinalizado em |
| --- | --- | --- | --- |
| LLM | `qwen-latest` via hub | `EchoLLM` determinista | `/readyz`, console, logs |
| Embeddings | `Qwen3-Embedding-0.6B` | `HashingEmbedder` determinista | idem |
| Tracing | Langfuse | `NoopTracer` | idem |
| Banco | PostgreSQL + pgvector (HNSW) | SQLite + cosseno em memoria | idem |
| Midia | FFmpeg/WhisperX/OCR/cenas | importacao de JSON | `/adwatch/capabilities` |

A degradacao e sempre **explicita**: nunca silenciosa, nunca disfarcada de sucesso.
Isso e o que permite a suite de testes rodar offline e o desenvolvedor trabalhar fora
da rede corporativa sem simular nada.

## 6. Runtimes de agente

| runtime | quando |
| --- | --- |
| `direct` | uma chamada de LLM — o mais barato e o mais previsivel |
| `langgraph` | grafo `prepare → plan → act ⇄ observe → reflect → finalize`, com limite de iteracoes |
| `deepagent` | Deep-Agent Harness: planejamento, sub-agentes, sistema de arquivos virtual |

O grafo LangGraph fala com o `LLMPort`, nao com um modelo LangChain — o hexagono
permanece intacto e o runtime e testavel com o `EchoLLM`. O Deep-Agent Harness precisa
de um `BaseChatModel`, entao usa `ChatOpenAI` apontado ao hub; quando a lib ou a chave
faltam, `supports()` devolve `False` e o container cai para `langgraph`, registrando a
degradacao.

## 7. AdWatch: a decisao que muda o custo

O caminho ingenuo seria mandar o video para o modelo multimodal e perguntar onde estao
os comerciais. Isso e caro, lento e nao explicavel.

Como **o texto procurado ja e conhecido**, o problema vira matching temporal
multimodal. A supervisao explicita muda a arquitetura:

```text
retrieval barato (janelas + fingerprints)  →  TOP-K  →  rerank  →  fusao de score
                                                                        │
                                            somente a faixa 0.60–0.90 chega ao VLM
```

O modelo de 27B roda em uma fracao dos candidatos, e cada deteccao carrega a evidencia
que a justifica (`speech_match`, `semantic_match`, `ocr_match`, `visual_match`,
`duration_match`, ordem). "Achei porque o texto bate 0.94, o OCR bate 0.88 e a marca
aparece" e auditavel; "o modelo achou que sim" nao e.

## 8. Escala e Kubernetes

* **Stateless**: nenhum estado de sessao em memoria — escala por replicas.
* **Async ponta a ponta**: nenhum I/O bloqueante no caminho da requisicao.
* **Boot resiliente**: a aplicacao sobe com o provedor fora do ar; `/readyz` reporta.
* **`/healthz` nunca toca no banco** — liveness que depende de dependencia derruba pod
  saudavel em cascata.
* **Encerramento gracioso**: `preStop` de 5s, `lifespan` fecha pool e faz flush do tracer.
* **12-factor**: toda configuracao por ambiente; a imagem nao carrega nada mutavel.

## 9. Decisoes registradas

Ver `docs/adr/`.
