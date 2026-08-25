# SPEC-0008 — Observabilidade

> **Status:** aceito · **Depende de:** SPEC-0000 · **Normativo.**

## 1. Camadas

| Camada | Tecnologia | Onde |
| --- | --- | --- |
| Tracing de IA | **Langfuse 4** (OpenTelemetry) | `adapters/observability/langfuse_tracer.py` |
| Logs estruturados | structlog (JSON em prod) | `config/logging.py` |
| Metricas | prometheus-client em `/metrics` | `adapters/observability/metrics.py` |
| Auditoria de negocio | `agent_runs` + `run_steps` no PostgreSQL | persistencia |

## 2. Convencao de traces

```text
trace  module.invoke:<slug>
 ├─ span  guardrail.input      (rules, findings, blocked)
 ├─ span  prompt.render        (prompt_slug, variables)
 ├─ span  runtime.<runtime>
 │    ├─ generation  llm.chat  (model, usage, cost_usd, latency)
 │    └─ span        tool.<nome>
 └─ span  guardrail.output
```
Atributos obrigatorios do trace: `module_slug`, `run_id`, `tenant_id`, `actor`,
`environment`, `version`. `trace_id` e gravado em `AgentRun.trace_id` e devolvido no
header `X-Trace-Id`.

Scores automaticos: `guardrail_blocked` (0/1), `latency_ms`, `cost_usd`.

## 3. `NoopTracer`

Implementa `TracerPort` inteiro sem efeito. E o padrao quando
`langfuse_enabled=false` ou faltam credenciais. **O codigo de negocio nunca verifica
se ha tracer** — sempre existe um.

Se `Langfuse.auth_check()` falhar no boot, o adaptador degrada para `NoopTracer`,
registra WARNING e reporta `degraded` em `/readyz`. Falha de telemetria nunca derruba
uma requisicao.

## 4. Metricas Prometheus

| Metrica | Tipo | Labels |
| --- | --- | --- |
| `lukato_http_requests_total` | counter | `method`, `path`, `status` |
| `lukato_http_request_duration_seconds` | histogram | `method`, `path` |
| `lukato_module_invocations_total` | counter | `module`, `status` |
| `lukato_module_latency_seconds` | histogram | `module`, `runtime` |
| `lukato_llm_tokens_total` | counter | `model`, `kind` (`prompt`/`completion`) |
| `lukato_llm_cost_usd_total` | counter | `model`, `module` |
| `lukato_guardrail_findings_total` | counter | `stage`, `kind`, `action` |
| `lukato_guardrail_blocks_total` | counter | `stage`, `policy` |
| `lukato_provider_errors_total` | counter | `provider`, `code` |

Cardinalidade: `path` usa o **template** da rota (`/api/v1/modules/{slug}`), nunca o
valor concreto.

## 5. Saude

* `GET /healthz` — liveness, resposta constante, **sem** tocar em dependencias.
* `GET /readyz` — readiness: banco, registry, provedor de LLM, embeddings, tracer.
  Cada componente reporta `ok | degraded | down`. `down` no banco → HTTP 503;
  `degraded` em provedores → HTTP 200 com o detalhe (a plataforma continua util offline).
* `GET /api/v1/health/providers` — detalhe por provedor, para o console.

## 6. Criterios de aceite

1. Sem credenciais Langfuse, a aplicacao sobe e `/readyz` reporta `tracer: degraded`.
2. Toda invocacao devolve `X-Run-Id`. `X-Trace-Id` sai **quando ha tracer ativo** —
   com o `NoopTracer` nao existe trace, e devolver um id inventado mandaria quem
   opera procurar um rastro que nunca foi gravado. O `AgentRun` e persistido em
   toda invocacao (inclusive nas bloqueadas), entao `X-Run-Id` e o identificador
   de correlacao que nunca falta.
3. `/metrics` expoe todas as metricas da secao 4 apos uma invocacao.
4. Erro do provedor de telemetria nao altera o status HTTP da requisicao de negocio.
