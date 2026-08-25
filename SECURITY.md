# Politica de seguranca — lukato

## Segredos

Nenhum segredo e versionado. Em ordem de preferencia:

1. **Kubernetes** — `ExternalSecrets` apontando para Vault / AWS Secrets Manager,
   materializando o Secret `lukato-secrets`; o Deployment consome por `secretKeyRef`.
2. **Docker/local** — arquivo `.env`, que esta no `.gitignore`.
3. Nunca em codigo, em `ConfigMap`, em imagem ou em log.

Segredos usados: chave do hub GPU (`LUKATO_LLM__API_KEY`), chave de embeddings,
`LUKATO_SECURITY__JWT_SECRET`, credenciais Langfuse e a URL do banco.

> A virtual key do hub GPU e restrita aos modelos entregues. Em caso de vazamento,
> solicite revogacao imediata ao time de plataforma (CCOE Arquitetura — GPU Hub).

O CI falha se encontrar no repositorio algo com a forma de `sk-…`, `AKIA…` ou uma
chave privada PEM.

## Superficie exposta

| Camada | Controle |
| --- | --- |
| Autenticacao | JWT HS256 ou API key (`lk_<prefix>_<secret>`, comparacao em tempo constante) |
| Autorizacao | RBAC por permissao granular; `Principal.can(...)` e o unico caminho |
| Entrada | guardrail de entrada por modulo: PII, segredos, prompt injection, tamanho, idioma |
| Saida | guardrail de saida: segredos, PII, JSON Schema, truncamento, juiz LLM |
| Transporte | HSTS e CSP em producao; CORS restrito por configuracao |
| Limite de uso | rate limit por principal + orcamento FinOps com `hard_stop` |

Senhas: bcrypt custo 12 com pre-hash SHA-256 (limite de 72 bytes do bcrypt).
Segredo de API key: `secrets.token_urlsafe(32)`, exibido **uma unica vez**.

## Antes de ir para producao

- [ ] `LUKATO_SECURITY__AUTH_ENABLED=true` — o boot **falha** em `prod` se estiver
      `false`, porque isso exporia toda a API como root anonimo
- [ ] `LUKATO_SECURITY__JWT_SECRET` forte, vindo do cofre (`openssl rand -hex 32`)
      — o boot recusa segredo com menos de 32 caracteres quando `env=prod` e auth ligada
- [ ] `LUKATO_SECURITY__CORS_ORIGINS` restrito — nunca `["*"]`
- [ ] `LUKATO_APP__DEBUG=false`, `LUKATO_DB__AUTO_FALLBACK=false`
- [ ] guardrails de entrada e saida vinculados a todos os modulos ativos
- [ ] orcamentos com `hard_stop` nos modulos expostos ao publico
- [ ] `NetworkPolicy` aplicada; egresso restrito ao banco, ao hub e ao Langfuse

## Reportar vulnerabilidade

Abra um chamado interno para o time responsavel pelo projeto. Nao abra issue publica
com detalhes de exploracao.
