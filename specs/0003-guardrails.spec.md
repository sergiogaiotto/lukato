# SPEC-0003 — Guardrails parametrizaveis (entrada → system prompt → saida)

> **Status:** aceito · **Depende de:** SPEC-0000 · **Normativo.**

## 1. Requisito

> *"os guardrail de entrada — system prompt — guardrail de saida devem ser
> parametrizaveis para todo e qualquer modulo criado"*

Consequencia normativa: **nenhum modulo pode chamar um LLM fora da trinca**.
A trinca vive em `ModuleDefinition.binding` (`ModuleBinding`) e e resolvida por
`ModuleComposer`; o caso de uso `InvokeModule` e o unico ponto de execucao.

```text
entrada do usuario
      │
      ▼
[1] GUARDRAIL DE ENTRADA   ← policy = binding.input_guardrail_id   (stage=input)
      │  bloqueio ⇒ run.status=BLOCKED, HTTP 422, nada e enviado ao provedor
      ▼
[2] SYSTEM PROMPT          ← prompt = binding.system_prompt_id, render(variables)
      │
      ▼
[3] RUNTIME                ← binding.model/temperature/max_tokens/tools
      │                      (langgraph | deepagent | direct)
      ▼
[4] GUARDRAIL DE SAIDA     ← policy = binding.output_guardrail_id  (stage=output)
      │  bloqueio ⇒ run.status=BLOCKED, resposta substituida pela mensagem da politica
      ▼
resposta + findings + usage + custo
```

Politica ausente (`None`) significa *sem restricao naquele estagio* — e uma escolha
explicita, registrada no run como `policy_id=null`, nunca um erro.

## 2. Motor (`domain/services/guardrail_engine.py`)

```python
class GuardrailEngine:                       # implementa GuardrailPort
    def __init__(self, evaluators: Sequence[GuardrailRuleEvaluator], *,
                 redaction_token: str = "[REDIGIDO]", fail_open: bool = False) -> None
    def register(self, evaluator: GuardrailRuleEvaluator) -> None
    async def apply(self, content: str, policy: GuardrailPolicy | None, *,
                    context: Json | None = None) -> GuardrailVerdict
```

Algoritmo de `apply`:
1. `policy is None` ou `policy.is_active is False` → veredito permissivo
   (`allowed=True`, `content` inalterado, `findings=[]`).
2. Ordena as regras habilitadas por `(order, id)`.
3. Para cada regra: localiza o avaliador por `kind`
   (ausente → `UnsupportedCapability`, ou finding `WARN` se `fail_open`).
4. Executa `evaluate(content_atual, rule, context)`.
   * Excecao do avaliador: `fail_open=True` → finding `WARN` e segue;
     `fail_open=False` → `GuardrailViolation`.
5. Aplica a acao do finding:
   | Acao | Efeito |
   | --- | --- |
   | `ALLOW` | nada |
   | `WARN` | registra o finding, conteudo intacto, `allowed` continua `True` |
   | `REDACT` | substitui os trechos indicados pelo `redaction_token`; segue para a proxima regra com o texto ja redigido |
   | `TRANSFORM` | substitui o conteudo pelo `finding.evidence` (texto transformado) |
   | `BLOCK` | `allowed=False`, **interrompe** a cadeia imediatamente |
6. Preenche `latency_ms`, `policy_id`, `original_content` e devolve `GuardrailVerdict`.

Determinismo obrigatorio: a mesma entrada + a mesma politica produzem o mesmo veredito
(exceto `LLM_JUDGE`, que depende do provedor e por isso e sempre a **ultima** regra
recomendada).

## 3. Avaliadores (`adapters/guardrails/`)

| `kind` | Arquivo | `config` | Comportamento |
| --- | --- | --- | --- |
| `regex_block` | `regex_rules.py` | `{"patterns":[str], "flags":"i"}` | finding se **qualquer** padrao casar; `span` do primeiro casamento |
| `regex_require` | `regex_rules.py` | `{"patterns":[str]}` | finding se **algum** padrao obrigatorio **nao** casar |
| `keyword_block` | `keywords.py` | `{"keywords":[str], "normalize":true, "whole_word":true}` | comparacao sem acento/caixa |
| `max_length` | `length.py` | `{"max_chars":int, "max_tokens":int}` | `TRANSFORM` trunca; `BLOCK` recusa |
| `min_length` | `length.py` | `{"min_chars":int}` | finding se menor |
| `pii_redact` | `pii.py` | `{"types":["cpf","cnpj","email","phone","credit_card","cep","ip","rg"]}` | redige o que casar; **CPF/CNPJ/cartao sao validados por digito verificador** para evitar falso positivo |
| `secret_scan` | `secrets_scan.py` | `{"extra_patterns":[str]}` | detecta `sk-…`, `AKIA…`, `ghp_…`, chaves privadas PEM, JWT, `Bearer` |
| `json_schema` | `schema_json.py` | `{"schema":{...}, "coerce":false}` | valida a saida como JSON contra JSON Schema (validador proprio, sem dependencia nova) |
| `language_allow` | `language.py` | `{"languages":["pt","en"], "min_confidence":0.5}` | heuristica por stopwords/caracteres — sem dependencia externa |
| `topic_block` | `topic.py` | `{"topics":[{"name":str,"terms":[str]}], "threshold":2}` | bloqueia por densidade de termos |
| `llm_judge` | `llm_judge.py` | `{"criteria":str, "threshold":0.5, "model":str|null}` | usa `LLMPort` com prompt de julgamento e resposta JSON `{"violates":bool,"score":float,"reason":str}`; timeout curto; falha → `WARN` |

`composite.py` expõe `build_default_evaluators(llm: LLMPort | None, settings) -> list[GuardrailRuleEvaluator]`.

Regras dos avaliadores:
* Nao alteram `content` diretamente — devolvem `GuardrailFinding` e o motor aplica a acao.
* `REDACT` retorna todos os intervalos em `finding.span` (primeiro) e o texto ja
  redigido em `finding.evidence`.
* Regex sao compilados com cache (`functools.lru_cache`) e **timeout logico**:
  padroes com mais de 500 caracteres sao rejeitados na validacao da politica.

## 4. Politicas pre-carregadas (seed)

| slug | stage | conteudo |
| --- | --- | --- |
| `entrada-padrao` | input | `max_length` (32k, BLOCK) · `secret_scan` (REDACT) · `pii_redact` (REDACT: cpf,cnpj,email,phone,credit_card) · `keyword_block` de prompt injection (`"ignore as instrucoes"`, `"reveal your system prompt"`, …) com BLOCK |
| `entrada-estrita` | input | tudo acima + `language_allow` (pt,en) + `topic_block` |
| `saida-padrao` | output | `secret_scan` (BLOCK) · `pii_redact` (REDACT) · `max_length` (32k, TRANSFORM) |
| `saida-json` | output | `json_schema` (BLOCK) + `max_length` |
| `saida-auditada` | output | `saida-padrao` + `llm_judge` (criterio: sem conselho financeiro/juridico definitivo) |

## 5. Observabilidade

Cada `apply` gera um span `guardrail.<stage>` com atributos
`policy`, `rules_evaluated`, `findings`, `blocked`, `latency_ms`, e vira um `RunStep`
de `kind=GUARDRAIL_IN|GUARDRAIL_OUT`. Um bloqueio adiciona um score Langfuse
`guardrail_blocked = 1`.

## 6. Criterios de aceite

1. Criar um modulo sem nenhuma politica funciona (trinca opcional, comportamento permissivo).
2. Vincular `entrada-padrao` a qualquer modulo bloqueia um CPF valido injetado no input
   **antes** de qualquer chamada de provedor (verificavel: o adaptador de LLM nao e chamado).
3. `saida-json` rejeita resposta que nao valide contra o schema.
4. Trocar a politica de um modulo em tempo de execucao muda o comportamento sem redeploy.
5. `fail_open=True` converte falha interna de regra em `WARN` e nao bloqueia.
6. Toda regra tem teste de unidade com caso positivo e negativo.
