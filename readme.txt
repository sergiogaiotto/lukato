===============================================================================
 lukato 1.0.0 - GUIA OPERACIONAL
 Ecossistema de agentes de IA modulares (building blocks)
===============================================================================

 Projeto ....... lukato
 Versao ........ 1.0.0
 Abordagem ..... Spec-Driven Development (SDD)
 Arquitetura ... Hexagonal (Ports & Adapters)
 Linguagem ..... Python 3.11
 Framework ..... FastAPI (OpenAPI 3.1 / Swagger)
 Agentes ....... LangGraph + Deep-Agent Harness (deepagents)
 LLM ........... qwen-latest / openai-gpt-oss-20b (hub GPU, API OpenAI-compativel)
 Embeddings .... Qwen/Qwen3-Embedding-0.6B (1024 dim, colecao pgvector agente_evidence)
 Banco ......... PostgreSQL 16 + pgvector (fallback SQLite em dev e testes)
 UI ............ Jinja2 (template engine), 3 colunas, menu recolhivel
 Observab. ..... Langfuse + structlog + Prometheus
 Deploy ........ Docker multi-stage non-root + Kustomize (Kubernetes)


-------------------------------------------------------------------------------
 1. O QUE E
-------------------------------------------------------------------------------

lukato e uma plataforma onde cada funcionalidade (autenticacao, processamento,
FinOps, conhecimento, deteccao de comerciais em video) e um BUILDING BLOCK
independente e fracamente acoplado.

O requisito central do projeto e atendido pelo nucleo: para TODO e QUALQUER
modulo criado, a trinca

      guardrail de entrada  ->  system prompt  ->  guardrail de saida

e parametrizavel, versionada e trocavel em tempo de execucao, sem redeploy e
sem escrever codigo. Criar um agente novo significa criar uma definicao de
modulo (uma linha no banco), nao um arquivo Python.


-------------------------------------------------------------------------------
 2. PRE-REQUISITOS
-------------------------------------------------------------------------------

 Obrigatorio:
   - Python 3.11 (3.12 tambem funciona)

 Opcional:
   - Docker + Docker Compose  (stack completa com PostgreSQL/pgvector)
   - kubectl + kustomize      (implantacao em Kubernetes)
   - FFmpeg, WhisperX, PaddleOCR, PySceneDetect  (pipeline de video do AdWatch;
     tudo em requirements-media.txt, e tudo OPCIONAL)

 IMPORTANTE: a aplicacao sobe e e util SEM chave de LLM, SEM PostgreSQL, SEM
 GPU e SEM rede. Nesse modo ela usa adaptadores deterministas locais e informa
 o que esta degradado em /readyz. Isso e proposital: o ambiente de
 desenvolvimento nao pode depender da rede corporativa.


-------------------------------------------------------------------------------
 3. INSTALACAO LOCAL
-------------------------------------------------------------------------------

   git clone <repositorio> && cd lukato

   make install-dev        # cria .venv e instala runtime + ferramentas
   make env                # copia .env.example para .env
   # edite .env e preencha LUKATO_LLM__API_KEY se quiser usar o hub de verdade

   make seed               # popula prompts, guardrails, modulos e catalogo demo
   make run                # sobe em http://localhost:8000

 Sem Make:

   python3 -m venv .venv
   .venv/bin/pip install -r requirements-dev.txt
   cp .env.example .env
   PYTHONPATH=src .venv/bin/python -m lukato.interfaces.cli seed
   PYTHONPATH=src .venv/bin/uvicorn lukato.main:app --host 0.0.0.0 --port 8000


-------------------------------------------------------------------------------
 4. ENDERECOS
-------------------------------------------------------------------------------

   http://localhost:8000/                    console web (UI)
   http://localhost:8000/api/docs            Swagger UI
   http://localhost:8000/api/redoc           ReDoc
   http://localhost:8000/api/openapi.json    contrato OpenAPI 3.1
   http://localhost:8000/healthz             liveness  (nao toca em dependencias)
   http://localhost:8000/readyz              readiness (banco, LLM, embeddings, tracer)
   http://localhost:8000/metrics             metricas Prometheus


-------------------------------------------------------------------------------
 5. DOCKER
-------------------------------------------------------------------------------

   make up            # sobe postgres+pgvector e a aplicacao
   make logs          # acompanha os logs
   make ps            # estado
   make down          # derruba e limpa volumes

   make docker-build  # somente a imagem  (lukato:1.0.0)
   make docker-run    # roda a imagem isolada com o .env local

 Rede restrita (proxy corporativo que bloqueia o CDN do Docker Hub):
 a imagem base e parametrizavel, entao aponte para o espelho interno --

   make docker-build-mirror PYTHON_IMAGE=<registry-interno>/python:3.11-slim-bookworm

 Serve qualquer imagem Debian bookworm com Python 3.11 (o Dockerfile usa apt).
 O build tambem precisa alcancar deb.debian.org para instalar curl, libpq5 e
 tini; se o proxy bloquear o repositorio Debian, aponte o apt para o espelho
 interno de voces.

 Perfil de observabilidade (Langfuse local em http://localhost:3000):

   docker compose --profile obs up -d

 A imagem roda como usuario nao-root (uid 10001), com filesystem raiz somente
 leitura, tini como PID 1 e HEALTHCHECK em /healthz. O entrypoint aceita:

   serve (padrao) | migrate | seed | shell


-------------------------------------------------------------------------------
 6. KUBERNETES
-------------------------------------------------------------------------------

   make k8s-render                       # revisa os manifestos renderizados
   kubectl apply -k deploy/k8s/overlays/dev
   kubectl apply -k deploy/k8s/overlays/prod

 O que esta incluso: Namespace, ServiceAccount, ConfigMap, Deployment (probes,
 recursos, securityContext restrito, topology spread, preStop), Service, HPA v2,
 PodDisruptionBudget, Ingress, NetworkPolicy, Job de migracao (hook PreSync do
 ArgoCD) e ServiceMonitor.

 SEGREDOS: deploy/k8s/base/secret.example.yaml contem apenas PLACEHOLDERS e nao
 entra no kustomization. Em producao use ExternalSecrets/Vault. Nenhum segredo
 real e versionado.


-------------------------------------------------------------------------------
 7. CONFIGURACAO
-------------------------------------------------------------------------------

 Tudo por variavel de ambiente. Prefixo LUKATO_ e aninhamento com "__":

   LUKATO_LLM__MODEL=qwen-latest        ->   settings.llm.model

 Grupos: APP, DB, LLM, EMBEDDING, GUARDRAILS, OBSERVABILITY, SECURITY, FINOPS,
 ADWATCH. A lista completa, comentada, esta em .env.example.

 Variaveis criticas:

   LUKATO_LLM__BASE_URL         https://hub-gpus-lab.usto.re/v1
   LUKATO_LLM__API_KEY          <<< SEGREDO — cofre corporativo, nunca no git
   LUKATO_LLM__MODEL            qwen-latest
   LUKATO_EMBEDDING__BASE_URL   https://hub-gpus.claro.com.br/embed06b/v1
   LUKATO_EMBEDDING__MODEL      Qwen/Qwen3-Embedding-0.6B
   LUKATO_EMBEDDING__DIMENSIONS 1024
   LUKATO_DB__URL               postgresql+asyncpg://user:senha@host:5432/lukato
   LUKATO_SECURITY__JWT_SECRET  <<< SEGREDO — openssl rand -hex 32

 AVISO SOBRE A DIMENSAO DO EMBEDDING: mudar LUKATO_EMBEDDING__DIMENSIONS exige
 re-embeddar a colecao pgvector inteira (agente_evidence). Fazer isso com dados
 em producao derruba a busca semantica ate o reindex terminar. A aplicacao
 detecta a divergencia e recusa gravar, em vez de corromper a colecao.

 AVISO SOBRE A CHAVE DO HUB: guarde em cofre corporativo. Nunca versione. Em
 caso de vazamento, solicite revogacao imediata ao time de plataforma.


-------------------------------------------------------------------------------
 8. COMO CRIAR UM MODULO (BUILDING BLOCK)
-------------------------------------------------------------------------------

 CAMINHO A - sem escrever codigo (o caminho normal).
 Crie uma definicao sobre a classe generica "processing":

   POST /api/v1/modules
   {
     "slug": "triagem-atendimento",
     "name": "Triagem de atendimento",
     "kind": "agent",
     "runtime": "langgraph",
     "binding": {
       "input_guardrail_id":  "<id da politica de entrada>",
       "system_prompt_id":    "<id do system prompt>",
       "output_guardrail_id": "<id da politica de saida>",
       "model": "qwen-latest",
       "temperature": 0.2,
       "tools": ["knowledge_search"]
     },
     "status": "active"
   }

 Invocar:

   POST /api/v1/modules/triagem-atendimento/invoke
   { "input": "cliente quer cancelar a internet", "variables": {"canal": "voz"} }

 CAMINHO B - com codigo (quando ha logica propria de verdade):

   from lukato.modules.base import BaseModule, ModuleRequest, ModuleResponse
   from lukato.modules.registry import register_module

   @register_module
   class MeuModulo(BaseModule):
       slug = "meu-modulo"
       name = "Meu modulo"
       kind = ModuleKind.AGENT
       capabilities = ("chat",)

       async def handle(self, request, ctx) -> ModuleResponse:
           ...

 Distribua como pacote com entry point no grupo "lukato.modules". O nucleo o
 descobre sozinho, sem nenhuma alteracao no codigo da plataforma.


-------------------------------------------------------------------------------
 9. GUARDRAILS
-------------------------------------------------------------------------------

 Tipos de regra disponiveis:

   regex_block      regex_require    keyword_block    pii_redact
   secret_scan      max_length       min_length       json_schema
   language_allow   topic_block      llm_judge

 Acoes: allow, warn, redact, transform, block.
 Severidades: low, medium, high, critical.

 Politicas pre-carregadas pelo seed:

   entrada-padrao    tamanho, segredos, PII, prompt injection
   entrada-estrita   entrada-padrao + idioma + topicos proibidos
   saida-padrao      segredos, PII, truncamento
   saida-json        validacao de JSON Schema
   saida-auditada    saida-padrao + juiz LLM

 Um bloqueio no guardrail de ENTRADA acontece ANTES de qualquer chamada ao
 provedor de LLM: o texto barrado nunca sai da plataforma.


-------------------------------------------------------------------------------
 10. ADWATCH - DETECCAO DE COMERCIAIS EM VIDEO
-------------------------------------------------------------------------------

 Como o texto dos comerciais ja e conhecido, o problema e matching temporal
 multimodal, nao classificacao aberta de video. O modelo multimodal entra no
 FIM do funil, como juiz.

   1. catalogo de comerciais (CRUD)      POST /api/v1/adwatch/commercials
   2. registrar a midia                  POST /api/v1/adwatch/media
   3. transcricao:
        - com FFmpeg+WhisperX            POST /api/v1/adwatch/media/{id}/ingest
        - ou importando JSON             POST /api/v1/adwatch/media/{id}/transcript
   4. detectar                           POST /api/v1/adwatch/media/{id}/detect
   5. revisar                            GET  /api/v1/adwatch/detections

 Score composto (pesos configuraveis, precisam somar 1.0):

   S = 0.40*lexico + 0.25*semantico + 0.15*ocr + 0.15*visual + 0.05*duracao

   S >= 0.90          aceita automaticamente
   0.60 <= S < 0.90   juiz multimodal decide
   S <  0.60          rejeita

 O caminho de importacao de transcricao (formato WhisperX) executa o pipeline
 inteiro sem FFmpeg, sem GPU e sem rede - e o caminho usado nos testes.


-------------------------------------------------------------------------------
 11. QUALIDADE
-------------------------------------------------------------------------------

   make lint     ruff
   make type     mypy (estrito em domain/ e application/)
   make test     pytest (roda offline, sem PostgreSQL e sem rede)
   make cov      cobertura
   make check    lint + type + test

 Ha um teste de arquitetura que falha se a regra hexagonal for violada, ou seja,
 se domain/ passar a importar sqlalchemy, fastapi, httpx, openai, langgraph,
 langfuse ou jinja2.

 Provas executaveis
 ------------------
 Duas delas, para quando ler o codigo nao basta. Cada uma monta o proprio banco
 descartavel e nao toca no seu: rode quantas vezes quiser, com ou sem .env.

   python scripts/prova_trinca.py
       O requisito central, em 7 asercoes. O LLM e um EchoLLM instrumentado que
       CONTA chamadas: quando o guardrail de entrada bloqueia, o contador fica em
       zero, e "o guardrail bloqueia antes do provedor" deixa de ser afirmacao e
       vira evidencia. Prova tambem que duas definicoes sobre a MESMA classe
       produzem comportamentos diferentes so trocando o binding.

   python scripts/prova_adwatch.py
       O funil do AdWatch inteiro sem FFmpeg, sem WhisperX, sem GPU e sem rede,
       pelo caminho de importacao de transcricao. Imprime a decomposicao do score
       parcela por parcela, contra os pesos da SPEC-0010. O comercial presente
       para em needs_review porque falta OCR: e o pipeline obedecendo a 3.6, nao
       um defeito.


-------------------------------------------------------------------------------
 12. BANCO DE DADOS
-------------------------------------------------------------------------------

   make migrate                       aplica as migracoes
   make migration m="mensagem"        cria migracao autogerada
   make downgrade                     volta uma migracao

 Em dev, sem PostgreSQL disponivel e com LUKATO_DB__AUTO_FALLBACK=true, a
 aplicacao cai para SQLite automaticamente. A busca vetorial passa a ser feita
 em memoria (numpy) em vez de usar o indice HNSW do pgvector.


-------------------------------------------------------------------------------
 13. ESTRUTURA DE DIRETORIOS
-------------------------------------------------------------------------------

   specs/            especificacoes normativas (SDD) - o codigo obedece a elas
   docs/             arquitetura, ADRs, notas de biblioteca
   src/lukato/
     domain/         nucleo puro: modelos, portas, servicos (zero I/O)
     application/    casos de uso
     adapters/       driven: persistencia, LLM, embeddings, guardrails,
                     runtime, observabilidade, midia, seguranca
     interfaces/     driving: HTTP (API v1), UI (Jinja2), CLI
     modules/        building blocks + registry
   migrations/       Alembic
   deploy/k8s/       Kustomize (base + overlays dev e prod)
   tests/            unit, integration, contract
   scripts/          entrypoint do container e utilitarios


-------------------------------------------------------------------------------
 14. SOLUCAO DE PROBLEMAS
-------------------------------------------------------------------------------

 "A aplicacao subiu mas as respostas parecem ecoar a pergunta"
   Nao ha LUKATO_LLM__API_KEY configurada. O adaptador caiu para o modo echo.
   Confira em GET /readyz -> providers.llm.

 "A busca semantica devolve resultados ruins"
   O provedor de embeddings caiu para o modo hashing (sem qualidade semantica).
   Confira GET /readyz -> providers.embeddings e a chave/URL de embeddings.

 "Erro de dimensao ao gravar embeddings"
   LUKATO_EMBEDDING__DIMENSIONS diverge da dimensao ja gravada na colecao.
   Re-embede a colecao inteira ou volte a dimensao anterior.

 "Timeout ou 000 ao chamar o hub"
   hub-gpus-lab.usto.re e hub-gpus.claro.com.br sao hosts internos da rede
   corporativa. Fora dela nao respondem. Use o modo offline para desenvolver.

 "429 do provedor"
   Limite de requisicoes. O adaptador ja faz retry com backoff exponencial;
   reduza a concorrencia ou solicite aumento de cota.

 "PostgreSQL indisponivel no boot"
   Com LUKATO_DB__AUTO_FALLBACK=true cai para SQLite e registra WARNING.
   Em producao use false para falhar rapido.


-------------------------------------------------------------------------------
 15. SEGURANCA - LEIA ANTES DE IR PARA PRODUCAO
-------------------------------------------------------------------------------

   [ ] LUKATO_SECURITY__AUTH_ENABLED=true
   [ ] LUKATO_SECURITY__JWT_SECRET forte (openssl rand -hex 32), vindo do cofre
   [ ] LUKATO_SECURITY__CORS_ORIGINS restrito (nunca ["*"])
   [ ] LUKATO_APP__DEBUG=false e LUKATO_APP__ENV=prod
   [ ] LUKATO_DB__AUTO_FALLBACK=false
   [ ] chaves de LLM/embeddings/Langfuse via Secret do Kubernetes ou Vault
   [ ] .env fora do controle de versao (ja esta no .gitignore)
   [ ] guardrails de entrada e saida vinculados a todos os modulos ativos
   [ ] orcamentos FinOps com hard_stop nos modulos expostos ao publico

===============================================================================
 Documentacao completa: README.md, specs/ e docs/ARCHITECTURE.md
===============================================================================
