# SPEC-0006 — Identidade, acesso e seguranca

> **Status:** aceito · **Depende de:** SPEC-0000 · **Normativo.**

## 1. Modelo de acesso

Papeis: `root` > `admin` > `operator` > `viewer`, mapeados para permissoes granulares
(`ROLE_PERMISSIONS`). `Principal.can(permission)` e a **unica** forma de autorizar;
nenhum endpoint compara papeis diretamente.

| Papel | Permissoes |
| --- | --- |
| `root`, `admin` | todas (`admin:*`) |
| `operator` | todos os `*:read` + `module:invoke` + `knowledge:write` |
| `viewer` | somente `*:read` |

## 2. Autenticacao

Dois esquemas, ambos declarados no OpenAPI:
* `Authorization: Bearer <JWT>` — HS256, `sub`, `role`, `tenant`, `exp`, `iat`, `iss=lukato`.
* `X-API-Key: lk_<prefix>_<secret>` — o prefixo indexa a linha; o segredo e comparado
  contra `hashed_secret` (bcrypt) em tempo constante.

`security.auth_enabled=false` (padrao em dev) resolve `Principal.anonymous_root()`.
Em producao (`app.env=prod`), `auth_enabled=false` e **erro de configuracao** no boot.

## 3. Senhas e segredos

* `BcryptHasher` — bcrypt custo 12; pre-hash SHA-256 para respeitar o limite de 72 bytes.
* Segredo de API key: `secrets.token_urlsafe(32)`, exibido **uma unica vez** na criacao.
* `SecretStr` em toda a configuracao; `__repr__` nunca imprime segredo.
* Comparacoes de segredo com `secrets.compare_digest`.

## 4. Middlewares HTTP

| Middleware | Funcao |
| --- | --- |
| `RequestIdMiddleware` | gera/propaga `X-Request-ID`, injeta no contexto de log |
| `AuthMiddleware` (dependencia) | resolve `Principal`; 401 quando ausente/invalido |
| `SecurityHeadersMiddleware` | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `CSP`, HSTS em prod |
| `RateLimitMiddleware` | janela deslizante por principal via `CachePort` (in-memory por padrao) |
| `TimingMiddleware` | `X-Response-Time-ms` + metrica Prometheus |

CORS conforme `security.cors_origins`; em producao `["*"]` gera aviso no boot.

## 5. API `/api/v1/identity`

`POST /login` · `POST /token/refresh` · `GET /me` ·
`GET|POST /users` · `GET|PUT|DELETE /users/{id}` ·
`GET|POST /api-keys` · `DELETE /api-keys/{id}` · `POST /api-keys/{id}/rotate`.

## 6. Criterios de aceite

1. `viewer` recebe 403 ao invocar um modulo; `operator` recebe 200.
2. API key expirada ou inativa → 401.
3. Segredo de API key nunca reaparece em nenhuma resposta apos a criacao.
4. Com `auth_enabled=false`, todas as rotas respondem como `root` anonimo.
5. `app.env=prod` + `auth_enabled=false` impede o boot com mensagem clara.
