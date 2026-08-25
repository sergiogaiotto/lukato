"""Contrato OpenAPI 3.1 publicado em `/api/openapi.json` (SPEC-0000 secao 11).

O documento OpenAPI e a **interface publica** do lukato: e ele que gera cliente, que
alimenta o Swagger e que qualquer integrador le antes de escrever a primeira linha.
Por isso ele e verificado como contrato, e nao como documentacao decorativa:

* a estrutura obedece ao OpenAPI 3.1 — versao, `info`, `paths`, `components`;
* as dez tags da SPEC-0000 secao 11 estao todas declaradas e descritas, e nenhuma
  operacao usa uma tag fora dessa lista;
* os dois esquemas de seguranca (`bearerAuth` e `apiKeyAuth`) sao declarados e
  exigidos na raiz, com as rotas que **emitem** credencial explicitamente liberadas;
* **toda** operacao tem `summary` e ao menos uma resposta documentada — um endpoint
  sem isso e um endpoint que ninguem consegue usar sem ler o codigo;
* nenhum schema expoe campo sensivel. Um `password_hash` no contrato seria um convite
  publicado, mesmo que a rota nunca o devolvesse.

O ultimo teste fecha o ciclo: o mesmo documento e exportavel para arquivo pela CLI
(`lukato openapi --out`), que e como o contrato entra no controle de versao e como
qualquer quebra aparece no diff da revisao.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from lukato.interfaces.cli import main as cli_main
from lukato.interfaces.http.openapi import OPENAPI_TAGS, SECURITY_SCHEMES
from lukato.main import OPENAPI_URL

pytestmark = [pytest.mark.integration, pytest.mark.contract]

METODOS_HTTP = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
"""Chaves de um Path Item que sao operacoes, segundo o OpenAPI 3.1."""

TAGS_NORMATIVAS = {
    "sistema",
    "modulos",
    "prompts",
    "guardrails",
    "execucoes",
    "conhecimento",
    "finops",
    "identidade",
    "adwatch",
    "registry",
}
"""Os dez grupos de tags da SPEC-0000 secao 11."""

CAMPOS_SENSIVEIS = {"password_hash", "hashed_secret", "api_key", "jwt_secret"}
"""Nomes de propriedade que nenhum schema do contrato pode declarar."""

ROTAS_PUBLICAS = {
    ("/api/v1/identity/login", "post"),
    ("/api/v1/identity/token/refresh", "post"),
}
"""Operacoes que dispensam credencial: sao elas que a emitem."""


# --------------------------------------------------------------------------- #
# Aparato
# --------------------------------------------------------------------------- #
@pytest.fixture
def contrato(app: FastAPI) -> dict[str, Any]:
    """Documento OpenAPI gerado pela aplicacao montada nas fixtures."""
    return dict(app.openapi())


def _operacoes(documento: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Lista `(caminho, metodo, operacao)` de todas as operacoes do contrato."""
    encontradas: list[tuple[str, str, dict[str, Any]]] = []
    for caminho, item in documento["paths"].items():
        for metodo in METODOS_HTTP:
            operacao = item.get(metodo)
            if isinstance(operacao, dict):
                encontradas.append((caminho, metodo, operacao))
    return encontradas


def _chaves(node: Any) -> set[str]:
    """Nomes de chave de um JSON, em qualquer profundidade."""
    nomes: set[str] = set()
    if isinstance(node, dict):
        for chave, valor in node.items():
            nomes.add(str(chave))
            nomes |= _chaves(valor)
    elif isinstance(node, list):
        for item in node:
            nomes |= _chaves(item)
    return nomes


def _exemplos(node: Any) -> list[Any]:
    """Todo valor de `example`/`examples` declarado em qualquer nivel do documento."""
    encontrados: list[Any] = []
    if isinstance(node, dict):
        for chave, valor in node.items():
            if chave in {"example", "examples"}:
                encontrados.append(valor)
            else:
                encontrados.extend(_exemplos(valor))
    elif isinstance(node, list):
        for item in node:
            encontrados.extend(_exemplos(item))
    return encontrados


def _propriedades(node: Any) -> set[str]:
    """Nomes de propriedade declarados em qualquer profundidade de um schema."""
    nomes: set[str] = set()
    if isinstance(node, dict):
        propriedades = node.get("properties")
        if isinstance(propriedades, dict):
            nomes |= {str(chave) for chave in propriedades}
        for valor in node.values():
            nomes |= _propriedades(valor)
    elif isinstance(node, list):
        for item in node:
            nomes |= _propriedades(item)
    return nomes


# --------------------------------------------------------------------------- #
# Estrutura
# --------------------------------------------------------------------------- #
async def test_openapi_e_servido_em_api_openapi_json(client: AsyncClient) -> None:
    """O contrato e publicado no caminho normativo, em JSON."""
    resposta = await client.get(OPENAPI_URL)

    assert resposta.status_code == 200, resposta.text
    assert resposta.headers["content-type"].startswith("application/json")
    assert resposta.json()["openapi"].startswith("3.1")


async def test_documento_servido_e_igual_ao_gerado_pela_aplicacao(
    client: AsyncClient, contrato: dict[str, Any]
) -> None:
    """O JSON servido e exatamente o documento memoizado — nao ha duas verdades."""
    servido = (await client.get(OPENAPI_URL)).json()

    assert servido == contrato


def test_estrutura_minima_do_openapi_31(contrato: dict[str, Any]) -> None:
    """Versao, identidade e as tres secoes obrigatorias do documento."""
    assert contrato["openapi"] == "3.1.0"
    assert contrato["info"]["title"] == "lukato"
    assert contrato["info"]["version"] == "1.0.0"
    assert contrato["info"]["description"], "a descricao explica a trinca a quem chega"
    assert contrato["paths"], "um contrato sem caminho nao descreve nada"
    assert "components" in contrato
    assert contrato["servers"], "o servidor relativo serve localhost, cluster e proxy"


def test_toda_operacao_tem_operation_id_unico(contrato: dict[str, Any]) -> None:
    """`operationId` repetido quebra qualquer gerador de cliente."""
    identificadores = [operacao.get("operationId") for _, _, operacao in _operacoes(contrato)]

    assert all(identificadores), "toda operacao precisa de operationId"
    repetidos = {item for item in identificadores if identificadores.count(item) > 1}
    assert not repetidos, f"operationId repetido: {sorted(repetidos)}"


def test_os_caminhos_normativos_da_spec_estao_publicados(contrato: dict[str, Any]) -> None:
    """Os dez recursos da SPEC-0000 secao 11 aparecem sob `/api/v1`, mais as sondas."""
    caminhos = set(contrato["paths"])

    for prefixo in (
        "/api/v1/modules",
        "/api/v1/prompts",
        "/api/v1/guardrails",
        "/api/v1/runs",
        "/api/v1/knowledge",
        "/api/v1/finops",
        "/api/v1/identity",
        "/api/v1/adwatch",
        "/api/v1/registry",
        "/api/v1/health",
    ):
        assert any(caminho.startswith(prefixo) for caminho in caminhos), (
            f"nenhum caminho publicado sob '{prefixo}'"
        )
    assert {"/healthz", "/readyz", "/metrics"} <= caminhos, (
        "as sondas ficam na raiz para que o manifesto do cluster nao conheca a versao"
    )


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #
def test_contrato_declara_os_dez_grupos_de_tags(contrato: dict[str, Any]) -> None:
    """As dez tags normativas estao declaradas, na ordem da SPEC, e com descricao."""
    declaradas = [tag["name"] for tag in contrato["tags"]]

    assert set(declaradas) == TAGS_NORMATIVAS
    assert declaradas == [tag["name"] for tag in OPENAPI_TAGS], "a ordem organiza o Swagger"
    assert all(tag.get("description") for tag in contrato["tags"]), (
        "tag sem descricao nao ajuda quem navega o Swagger"
    )


def test_nenhuma_operacao_usa_tag_fora_do_catalogo(contrato: dict[str, Any]) -> None:
    """Uma tag nao declarada cria uma secao orfa na navegacao."""
    usadas = {tag for _, _, operacao in _operacoes(contrato) for tag in operacao.get("tags", [])}

    assert usadas <= TAGS_NORMATIVAS, f"tags fora do catalogo: {sorted(usadas - TAGS_NORMATIVAS)}"


def test_toda_operacao_declara_ao_menos_uma_tag(contrato: dict[str, Any]) -> None:
    """Operacao sem tag cai em um grupo `default` que nao existe na SPEC."""
    sem_tag = [
        f"{metodo.upper()} {caminho}"
        for caminho, metodo, operacao in _operacoes(contrato)
        if not operacao.get("tags")
    ]

    assert not sem_tag, f"operacoes sem tag: {sem_tag}"


# --------------------------------------------------------------------------- #
# Seguranca
# --------------------------------------------------------------------------- #
def test_contrato_declara_bearer_auth_e_api_key_auth(contrato: dict[str, Any]) -> None:
    """Os dois esquemas da SPEC-0006 secao 2 estao no documento, com a forma correta."""
    esquemas = contrato["components"]["securitySchemes"]

    assert set(esquemas) == set(SECURITY_SCHEMES) == {"bearerAuth", "apiKeyAuth"}
    assert esquemas["bearerAuth"]["type"] == "http"
    assert esquemas["bearerAuth"]["scheme"] == "bearer"
    assert esquemas["bearerAuth"]["bearerFormat"] == "JWT"
    assert esquemas["apiKeyAuth"]["type"] == "apiKey"
    assert esquemas["apiKeyAuth"]["in"] == "header"
    assert esquemas["apiKeyAuth"]["name"] == "X-API-Key"


def test_requisito_de_seguranca_global_aceita_qualquer_um_dos_dois(
    contrato: dict[str, Any],
) -> None:
    """Dois itens na lista significam OR: JWT **ou** chave de API, nunca os dois."""
    assert contrato["security"] == [{"bearerAuth": []}, {"apiKeyAuth": []}]


def test_rotas_que_emitem_credencial_dispensam_credencial(contrato: dict[str, Any]) -> None:
    """`/login` e `/token/refresh` anulam o requisito global com `security: []`."""
    for caminho, metodo in ROTAS_PUBLICAS:
        operacao = contrato["paths"][caminho][metodo]
        assert operacao.get("security") == [], (
            f"{metodo.upper()} {caminho} exigiria credencial para produzir credencial"
        )


# --------------------------------------------------------------------------- #
# Qualidade de cada operacao
# --------------------------------------------------------------------------- #
def test_toda_rota_tem_summary(contrato: dict[str, Any]) -> None:
    """Sem `summary` o Swagger mostra so o verbo e o caminho."""
    sem_resumo = [
        f"{metodo.upper()} {caminho}"
        for caminho, metodo, operacao in _operacoes(contrato)
        if not str(operacao.get("summary", "")).strip()
    ]

    assert not sem_resumo, f"operacoes sem summary: {sem_resumo}"


def test_toda_rota_documenta_ao_menos_uma_resposta(contrato: dict[str, Any]) -> None:
    """Uma operacao sem resposta documentada nao diz o que devolve."""
    sem_resposta = [
        f"{metodo.upper()} {caminho}"
        for caminho, metodo, operacao in _operacoes(contrato)
        if not operacao.get("responses")
    ]

    assert not sem_resposta, f"operacoes sem resposta documentada: {sem_resposta}"


def test_toda_resposta_documentada_tem_descricao(contrato: dict[str, Any]) -> None:
    """`description` e obrigatorio em um Response Object do OpenAPI 3.1."""
    sem_descricao = [
        f"{metodo.upper()} {caminho} -> {status}"
        for caminho, metodo, operacao in _operacoes(contrato)
        for status, resposta in operacao["responses"].items()
        if not str(resposta.get("description", "")).strip()
    ]

    assert not sem_descricao, f"respostas sem descricao: {sem_descricao}"


def test_toda_rota_de_negocio_documenta_uma_resposta_de_sucesso(
    contrato: dict[str, Any],
) -> None:
    """Cada operacao declara pelo menos um status 2xx."""
    sem_sucesso = [
        f"{metodo.upper()} {caminho}"
        for caminho, metodo, operacao in _operacoes(contrato)
        if not any(str(status).startswith("2") for status in operacao["responses"])
    ]

    assert not sem_sucesso, f"operacoes sem resposta de sucesso: {sem_sucesso}"


def test_a_invocacao_documenta_os_erros_normativos_da_trinca(
    contrato: dict[str, Any],
) -> None:
    """`POST /modules/{slug}/invoke` publica 402, 404, 409 e 422 (SPEC-0001 secao 4)."""
    respostas = set(contrato["paths"]["/api/v1/modules/{slug}/invoke"]["post"]["responses"])

    assert {"200", "402", "404", "409", "422"} <= respostas, (
        f"a rota de invocacao nao documenta todos os erros da trinca: {sorted(respostas)}"
    )


def test_o_envelope_de_erro_normativo_esta_no_contrato(contrato: dict[str, Any]) -> None:
    """O erro sai sempre como `{"error": {"code", "message", "details"}}`."""
    schemas = contrato["components"]["schemas"]

    assert "ErrorResponse" in schemas
    assert set(schemas["ErrorResponse"]["properties"]) == {"error"}
    assert set(schemas["ErrorBody"]["properties"]) == {"code", "message", "details"}


def test_o_envelope_de_lista_normativo_esta_no_contrato(contrato: dict[str, Any]) -> None:
    """Toda listagem responde `items/total/limit/offset`."""
    schemas = contrato["components"]["schemas"]
    paginas = [nome for nome in schemas if nome.startswith("Page_")]

    assert paginas, "nenhum schema de pagina foi gerado"
    for nome in paginas:
        assert set(schemas[nome]["properties"]) == {"items", "total", "limit", "offset"}, (
            f"o envelope de lista '{nome}' divergiu do normativo"
        )


# --------------------------------------------------------------------------- #
# Segredos
# --------------------------------------------------------------------------- #
def test_nenhum_schema_do_contrato_declara_campo_sensivel(contrato: dict[str, Any]) -> None:
    """Publicar `password_hash` no contrato seria um convite, mesmo sem rota que o devolva."""
    vazados = _propriedades(contrato.get("components", {}).get("schemas", {})) & CAMPOS_SENSIVEIS

    assert not vazados, f"schemas do contrato declaram campo sensivel: {sorted(vazados)}"


def test_nenhum_exemplo_do_contrato_carrega_campo_sensivel(contrato: dict[str, Any]) -> None:
    """Os exemplos do Swagger sao copiados por quem integra: nada de hash neles.

    A varredura olha os objetos de `example`/`examples`, e nao o texto inteiro do
    documento: uma **descricao** que diz "esta rota nunca devolve `password_hash`"
    e documentacao correta, nao vazamento.
    """
    vazados: set[str] = set()
    for exemplo in _exemplos(contrato):
        vazados |= _chaves(exemplo) & CAMPOS_SENSIVEIS

    assert not vazados, f"exemplos do contrato trazem campo sensivel: {sorted(vazados)}"


def test_o_contrato_nao_carrega_valor_de_segredo_da_configuracao(
    contrato: dict[str, Any],
) -> None:
    """Nenhum segredo de `Settings` pode escorrer para o documento publicado."""
    bruto = json.dumps(contrato, ensure_ascii=False)

    assert "segredo-de-teste-com-mais-de-32-chars" not in bruto
    assert "change-me" not in bruto


def test_o_schema_de_chave_criada_expoe_o_segredo_uma_unica_vez(
    contrato: dict[str, Any],
) -> None:
    """`ApiKeyCreatedOut` tem `secret`; `ApiKeyOut`, que e a leitura, nao tem."""
    schemas = contrato["components"]["schemas"]

    assert "secret" in schemas["ApiKeyCreatedOut"]["properties"]
    assert "secret" not in schemas["ApiKeyOut"]["properties"]


# --------------------------------------------------------------------------- #
# Exportacao pela CLI
# --------------------------------------------------------------------------- #
def test_cli_exporta_o_contrato_para_arquivo(tmp_path: Path) -> None:
    """`lukato openapi --out` grava o documento para versionar junto do codigo."""
    destino = tmp_path / "contrato" / "openapi.json"

    codigo = cli_main(["openapi", "--out", str(destino)])

    assert codigo == 0, "a exportacao precisa terminar com sucesso"
    assert destino.exists(), "o diretorio de destino e criado quando nao existe"
    documento = json.loads(destino.read_text(encoding="utf-8"))
    assert documento["openapi"] == "3.1.0"
    assert documento["info"]["title"] == "lukato"
    assert documento["paths"]
    assert set(documento["components"]["securitySchemes"]) == {"bearerAuth", "apiKeyAuth"}


def test_contrato_exportado_preserva_acentuacao(tmp_path: Path) -> None:
    """O arquivo e gravado em UTF-8 sem escapar acento: o diff fica legivel."""
    destino = tmp_path / "openapi.json"

    cli_main(["openapi", "--out", str(destino)])

    bruto = destino.read_text(encoding="utf-8")
    assert "\\u00e7" not in bruto, "acentos escapados tornam o diff ilegivel"
    assert bruto.endswith("\n"), "arquivo versionado termina com quebra de linha"


def test_contrato_exportado_e_igual_ao_servido(tmp_path: Path, contrato: dict[str, Any]) -> None:
    """O arquivo exportado descreve a mesma API que a aplicacao serve.

    Os `servers` podem divergir (o `root_path` da instalacao entra ali), entao a
    comparacao e sobre o que define a interface: caminhos, esquemas e seguranca.
    """
    destino = tmp_path / "openapi.json"
    cli_main(["openapi", "--out", str(destino)])

    exportado = json.loads(destino.read_text(encoding="utf-8"))

    assert set(exportado["paths"]) == set(contrato["paths"])
    assert exportado["components"]["securitySchemes"] == contrato["components"]["securitySchemes"]
    assert exportado["security"] == contrato["security"]
    assert [tag["name"] for tag in exportado["tags"]] == [tag["name"] for tag in contrato["tags"]]
