"""Personalizacao do OpenAPI 3.1 do lukato e exportacao do documento.

O contrato publicado precisa explicar o que a plataforma faz, nao apenas listar
rotas: a trinca **guardrail de entrada -> system prompt -> guardrail de saida** e
a regra que organiza todo o ecossistema, e quem le o Swagger deve encontra-la na
primeira tela.

Aqui tambem ficam os dois esquemas de seguranca declarados (`bearerAuth` e
`apiKeyAuth`) e a descricao de cada tag, para que a navegacao do Swagger espelhe
as SPECs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from lukato.config import get_logger

__all__ = [
    "API_DESCRIPTION",
    "API_TITLE",
    "API_VERSION",
    "OPENAPI_TAGS",
    "SECURITY_REQUIREMENT",
    "SECURITY_SCHEMES",
    "customize_openapi",
    "export_openapi",
]

_logger = get_logger(__name__)

API_TITLE: Final[str] = "lukato"
"""Titulo publicado no Swagger e no ReDoc."""

API_VERSION: Final[str] = "1.0.0"
"""Versao do contrato, igual a versao do pacote."""

OPENAPI_VERSION: Final[str] = "3.1.0"
"""Versao do OpenAPI emitida (SPEC-0000 secao 11)."""

API_DESCRIPTION: Final[str] = """
# lukato — ecossistema modular de agentes de IA

Plataforma de **building blocks**: cada funcionalidade e um modulo plugavel,
configurado por dados e executado sempre pelo mesmo caminho auditavel.

## A trinca obrigatoria

Todo modulo do lukato executa nesta ordem exata, sem excecao:

1. **Guardrail de entrada** — a politica ligada em `binding.input_guardrail_id`
   inspeciona o texto do usuario. Ela bloqueia injecao de prompt, redige dados
   pessoais e recusa o que estiver fora do escopo **antes** de qualquer token
   chegar ao modelo.
2. **System prompt** — o template de `binding.system_prompt_id` e renderizado
   com as variaveis do pedido. O prompt e versionado: mudar o texto cria uma
   versao nova, e a execucao registra qual delas foi usada.
3. **Guardrail de saida** — a politica de `binding.output_guardrail_id` avalia a
   resposta gerada. Segredo vazado, PII e formato invalido morrem aqui, nao no
   navegador do cliente.

Trocar qualquer uma das tres pecas e **configuracao**, nao codigo: duas
definicoes sobre a mesma classe de modulo, com bindings diferentes, sao dois
agentes diferentes.

## Convencoes da API

* Toda lista responde `{"items": [...], "total": N, "limit": N, "offset": N}`.
* Todo erro responde `{"error": {"code": ..., "message": ..., "details": {...}}}`
  com o cabecalho `X-Request-ID` para correlacao no log.
* O `code` do erro e **estavel**: programe contra ele, nunca contra a mensagem.
* Autenticacao por `Authorization: Bearer <jwt>` ou `X-API-Key: lk_<prefixo>_<segredo>`.
  Com `LUKATO_SECURITY__AUTH_ENABLED=false` (padrao em desenvolvimento) toda rota
  responde como um `root` anonimo.
* A instalacao funciona **offline**: sem rede, o LLM cai para `echo`, os
  embeddings para `hashing`, o tracer para `noop` e o banco para SQLite.
""".strip()
"""Descricao em portugues exibida no topo do Swagger."""

CONTACT: Final[dict[str, Any]] = {
    "name": "Equipe lukato",
    "url": "https://github.com/lukato",
}
"""Contato publicado no documento OpenAPI."""

LICENSE_INFO: Final[dict[str, Any]] = {"name": "Proprietary"}
"""Licenca declarada no contrato (espelha `pyproject.toml`)."""

OPENAPI_TAGS: Final[list[dict[str, Any]]] = [
    {
        "name": "sistema",
        "description": (
            "Liveness, readiness e detalhe dos provedores. `/healthz` responde sem "
            "tocar em dependencia alguma; `/readyz` sonda banco, registry, LLM, "
            "embeddings e tracer."
        ),
    },
    {
        "name": "modulos",
        "description": (
            "CRUD das definicoes de building block e a rota de invocacao, que "
            "executa a trinca guardrail de entrada / system prompt / guardrail de saida."
        ),
    },
    {
        "name": "prompts",
        "description": (
            "Biblioteca versionada de system prompts. Alterar o texto cria uma "
            "versao nova; o preview renderiza sem falhar quando falta variavel."
        ),
    },
    {
        "name": "guardrails",
        "description": (
            "Politicas de entrada e de saida, catalogo dos onze tipos de regra e o "
            "testador, que devolve o veredito completo sem persistir nada."
        ),
    },
    {
        "name": "execucoes",
        "description": (
            "Trilha auditavel das invocacoes: passos na ordem em que aconteceram, "
            "consumo de tokens, custo e latencia por passo."
        ),
    },
    {
        "name": "conhecimento",
        "description": (
            "Documentos, colecoes e busca semantica. A identidade do embedder "
            "acompanha cada colecao para impedir consulta incompativel."
        ),
    },
    {
        "name": "finops",
        "description": (
            "Custo apurado, serie temporal, tabela de precos por modelo e "
            "orcamentos com alerta e parada dura."
        ),
    },
    {
        "name": "identidade",
        "description": (
            "Login, renovacao de token, usuarios e chaves de API. O segredo de uma "
            "chave aparece uma unica vez, no momento da criacao."
        ),
    },
    {
        "name": "adwatch",
        "description": (
            "Catalogo de comerciais, ingestao de midia e deteccao multimodal com "
            "evidencias por modalidade (fala, semantica, OCR, visao e duracao)."
        ),
    },
    {
        "name": "registry",
        "description": (
            "Building blocks instalados nesta instancia, com capacidades e schema "
            "de configuracao de cada um."
        ),
    },
]
"""Tags do OpenAPI, na ordem em que devem aparecer no Swagger."""

SECURITY_SCHEMES: Final[dict[str, Any]] = {
    "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "JWT HS256 emitido por `POST /api/v1/identity/login`.",
    },
    "apiKeyAuth": {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "Chave `lk_<prefixo>_<segredo>` criada em `/api/v1/identity/api-keys`.",
    },
}
"""Esquemas de seguranca declarados no contrato (SPEC-0006 secao 2)."""

SECURITY_REQUIREMENT: Final[list[dict[str, list[str]]]] = [{"bearerAuth": []}, {"apiKeyAuth": []}]
"""Requisito global: qualquer um dos dois esquemas atende (OR, nao AND).

Declarar no nivel raiz liga o botao **Authorize** do Swagger para toda a API. Com
`security.auth_enabled=false` as rotas respondem sem credencial mesmo assim — o
contrato descreve a instalacao autenticada, que e a de producao.
"""


def _servers(app: FastAPI) -> list[dict[str, Any]]:
    """Servidor relativo, respeitando o `root_path` da instalacao.

    Usar URL relativa e deliberado: o mesmo documento serve para localhost, para
    o cluster e para um proxy com prefixo, sem precisar ser regerado.
    """
    root = (app.root_path or "").rstrip("/")
    return [{"url": root or "/", "description": "Servidor atual"}]


def customize_openapi(app: FastAPI) -> Callable[[], dict[str, Any]]:
    """Substitui `app.openapi` pelo gerador personalizado e o devolve.

    O documento e memoizado em `app.openapi_schema`, como o FastAPI faz: o
    Swagger pede o JSON a cada carregamento de pagina e regerar o esquema
    inteiro toda vez custaria caro em uma API deste tamanho.
    """

    def openapi() -> dict[str, Any]:
        """Gera (uma unica vez) o documento OpenAPI 3.1 do lukato."""
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=API_TITLE,
            version=API_VERSION,
            openapi_version=OPENAPI_VERSION,
            summary="Ecossistema modular de agentes de IA com guardrails parametrizaveis.",
            description=API_DESCRIPTION,
            routes=app.routes,
            tags=OPENAPI_TAGS,
            servers=_servers(app),
            contact=CONTACT,
            license_info=LICENSE_INFO,
        )
        components = schema.setdefault("components", {})
        components["securitySchemes"] = dict(SECURITY_SCHEMES)
        schema["security"] = [dict(item) for item in SECURITY_REQUIREMENT]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
    return openapi


def export_openapi(app: FastAPI, path: str | Path) -> None:
    """Grava o documento OpenAPI em `path`, identado e sem escapar acentos.

    Usado pela CLI (`lukato openapi`) para versionar o contrato junto do codigo,
    o que torna qualquer quebra visivel no diff da revisao.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = app.openapi()
    destination.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    _logger.info(
        "openapi_exported",
        path=str(destination),
        paths=len(document.get("paths", {})),
        version=document.get("info", {}).get("version", API_VERSION),
    )
