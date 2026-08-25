"""Identidade, autorizacao e segredos pela borda HTTP (SPEC-0006).

Tres invariantes governam este arquivo, e cada assert existe por causa de um deles:

* **a 401 nao conta nada.** E-mail inexistente, senha errada e conta desativada saem
  com o mesmo status e a mesma mensagem. Qualquer diferenca observavel transformaria
  o formulario de login em um verificador de e-mails cadastrados;
* **a autorizacao passa por `Principal.can`.** Um `viewer` recebe `403` ao invocar um
  modulo e um `operator` recebe `200` — a diferenca esta na permissao, nao no papel
  comparado a mao;
* **segredo nao volta duas vezes.** `password_hash` e `hashed_secret` nao podem
  aparecer em resposta nenhuma, e o teste varre o JSON **recursivamente** em vez de
  confiar nos campos que o schema declara.

A autenticacao comeca desligada (o padrao de desenvolvimento) e e ligada no meio do
teste, no mesmo processo, por :func:`_liga_autenticacao`. E deliberado: o cadastro
inicial e feito pelo root anonimo, exatamente como acontece em uma instalacao nova.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from lukato.application.container import Container
from lukato.application.use_cases.identity import INVALID_CREDENTIALS
from lukato.domain.types import Json
from tests.conftest import SeedIds

pytestmark = pytest.mark.integration

SENHA = "senha-de-teste-123"
"""Senha usada por todos os usuarios criados aqui (minimo de 8 caracteres)."""

CAMPOS_PROIBIDOS = frozenset({"password_hash", "hashed_secret", "jwt_secret", "api_key"})
"""Chaves que nenhuma resposta da API pode carregar (SPEC-0006 secao 3)."""

PASSADO = datetime(2020, 1, 1, tzinfo=UTC)
"""Data fixa no passado — nada aqui depende do relogio real."""


# --------------------------------------------------------------------------- #
# Utilitarios
# --------------------------------------------------------------------------- #
def _liga_autenticacao(container: Container) -> None:
    """Liga `security.auth_enabled` no processo em execucao, sem recriar a aplicacao."""
    container.settings.security.auth_enabled = True


def _chaves(payload: Any) -> set[str]:
    """Todas as chaves de um JSON, em qualquer profundidade."""
    encontradas: set[str] = set()
    if isinstance(payload, dict):
        for chave, valor in payload.items():
            encontradas.add(str(chave))
            encontradas |= _chaves(valor)
    elif isinstance(payload, list):
        for item in payload:
            encontradas |= _chaves(item)
    return encontradas


def _exige_sem_segredo(rotulo: str, payload: Any) -> None:
    """Falha se o JSON carregar qualquer campo sensivel, em qualquer nivel."""
    vazados = _chaves(payload) & CAMPOS_PROIBIDOS
    assert not vazados, f"a resposta de {rotulo} vazou {sorted(vazados)}"


async def _cria_usuario(client: AsyncClient, email: str, role: str) -> Json:
    """Cria um usuario (com a autenticacao ainda desligada) e devolve o cadastro."""
    resposta = await client.post(
        "/api/v1/identity/users",
        json={"email": email, "password": SENHA, "name": email.split("@")[0], "role": role},
    )
    assert resposta.status_code == 201, resposta.text
    return dict(resposta.json())


async def _login(client: AsyncClient, email: str, senha: str = SENHA) -> str:
    """Autentica e devolve o `access_token`."""
    resposta = await client.post("/api/v1/identity/login", json={"email": email, "password": senha})
    assert resposta.status_code == 200, resposta.text
    return str(resposta.json()["access_token"])


def _bearer(token: str) -> dict[str, str]:
    """Cabecalho `Authorization` do esquema `bearerAuth`."""
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Autenticacao desligada
# --------------------------------------------------------------------------- #
async def test_com_autenticacao_desligada_toda_rota_responde_como_root_anonimo(
    client: AsyncClient,
) -> None:
    """SPEC-0006 criterio 4: o padrao de desenvolvimento resolve o root anonimo."""
    resposta = await client.get("/api/v1/identity/me")

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["kind"] == "anonymous"
    assert corpo["role"] == "root"
    assert "admin:*" in corpo["permissions"]
    assert corpo["user"] is None
    _exige_sem_segredo("GET /identity/me", corpo)


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
async def test_login_feliz_devolve_token_com_papel_e_validade(client: AsyncClient) -> None:
    """O login troca e-mail e senha por um JWT com papel, inquilino e expiracao."""
    await _cria_usuario(client, "operador@lukato.local", "operator")

    resposta = await client.post(
        "/api/v1/identity/login",
        json={"email": "operador@lukato.local", "password": SENHA},
    )

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["access_token"]
    assert corpo["token_type"].lower() == "bearer"
    assert corpo["expires_in"] > 0
    assert corpo["role"] == "operator"
    _exige_sem_segredo("POST /identity/login", corpo)


async def test_as_duas_falhas_de_login_sao_indistinguiveis(client: AsyncClient) -> None:
    """SPEC-0006 secao 2 — e-mail inexistente e senha errada respondem igual.

    Mesmo status, mesmo codigo, mesma mensagem e os mesmos detalhes. Qualquer
    variacao observavel devolveria ao atacante o oraculo de enumeracao que a
    mensagem unica se recusa a dar.
    """
    await _cria_usuario(client, "existe@lukato.local", "viewer")

    email_desconhecido = await client.post(
        "/api/v1/identity/login",
        json={"email": "nao-existe@lukato.local", "password": SENHA},
    )
    senha_errada = await client.post(
        "/api/v1/identity/login",
        json={"email": "existe@lukato.local", "password": "senha-completamente-errada"},
    )

    assert email_desconhecido.status_code == senha_errada.status_code == 401
    assert email_desconhecido.json() == senha_errada.json(), (
        "as duas falhas precisam ser indistinguiveis para quem chama"
    )
    assert email_desconhecido.json()["error"]["message"] == INVALID_CREDENTIALS
    assert email_desconhecido.json()["error"]["code"] == "unauthorized"


async def test_conta_desativada_recebe_a_mesma_401_generica(client: AsyncClient) -> None:
    """A terceira falha tambem nao se distingue das outras duas."""
    criado = await _cria_usuario(client, "inativo@lukato.local", "operator")
    desativacao = await client.put(
        f"/api/v1/identity/users/{criado['id']}", json={"is_active": False}
    )
    assert desativacao.status_code == 200, desativacao.text

    resposta = await client.post(
        "/api/v1/identity/login", json={"email": "inativo@lukato.local", "password": SENHA}
    )

    assert resposta.status_code == 401
    assert resposta.json()["error"]["message"] == INVALID_CREDENTIALS


async def test_token_renovado_mantem_a_identidade(client: AsyncClient) -> None:
    """O refresh rele o usuario no banco e devolve outro token para o mesmo sujeito."""
    await _cria_usuario(client, "renova@lukato.local", "operator")
    token = await _login(client, "renova@lukato.local")

    resposta = await client.post("/api/v1/identity/token/refresh", json={"token": token})

    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["role"] == "operator"
    _exige_sem_segredo("POST /identity/token/refresh", resposta.json())


# --------------------------------------------------------------------------- #
# Autorizacao por permissao
# --------------------------------------------------------------------------- #
async def test_viewer_recebe_403_ao_invocar_e_operator_recebe_200(
    client: AsyncClient, container: Container, seeded: SeedIds
) -> None:
    """SPEC-0006 criterio 1 — a permissao decide, nao o papel comparado a mao."""
    await _cria_usuario(client, "viewer@lukato.local", "viewer")
    await _cria_usuario(client, "operator@lukato.local", "operator")
    _liga_autenticacao(container)

    token_viewer = await _login(client, "viewer@lukato.local")
    token_operator = await _login(client, "operator@lukato.local")
    corpo = {"input": "bom dia"}

    negado = await client.post(
        "/api/v1/modules/assistente/invoke", json=corpo, headers=_bearer(token_viewer)
    )
    permitido = await client.post(
        "/api/v1/modules/assistente/invoke", json=corpo, headers=_bearer(token_operator)
    )

    assert negado.status_code == 403, negado.text
    assert negado.json()["error"]["code"] == "forbidden"
    assert negado.json()["error"]["details"]["required_permission"] == "module:invoke"
    assert permitido.status_code == 200, permitido.text
    assert permitido.json()["output"]


async def test_viewer_continua_lendo_o_catalogo(
    client: AsyncClient, container: Container, seeded: SeedIds
) -> None:
    """O `viewer` perde a invocacao, nao a leitura: `*:read` continua valendo."""
    await _cria_usuario(client, "viewer@lukato.local", "viewer")
    _liga_autenticacao(container)
    token = await _login(client, "viewer@lukato.local")

    resposta = await client.get("/api/v1/modules", headers=_bearer(token))

    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["total"] >= 1


async def test_operator_nao_administra_usuarios(client: AsyncClient, container: Container) -> None:
    """Administrar identidade exige `admin:*`, que o operador nao tem."""
    await _cria_usuario(client, "operator@lukato.local", "operator")
    _liga_autenticacao(container)
    token = await _login(client, "operator@lukato.local")

    resposta = await client.get("/api/v1/identity/users", headers=_bearer(token))

    assert resposta.status_code == 403
    assert resposta.json()["error"]["details"]["required_permission"] == "admin:*"


async def test_requisicao_sem_credencial_com_autenticacao_ligada_devolve_401(
    client: AsyncClient, container: Container, seeded: SeedIds
) -> None:
    """Com a autenticacao ligada, credencial ausente e 401 — nunca root anonimo."""
    _liga_autenticacao(container)

    resposta = await client.get("/api/v1/modules")

    assert resposta.status_code == 401
    erro = resposta.json()["error"]
    assert erro["code"] == "unauthorized"
    assert erro["details"]["schemes"] == ["bearerAuth", "apiKeyAuth"]


# --------------------------------------------------------------------------- #
# Chaves de API
# --------------------------------------------------------------------------- #
async def test_ciclo_de_vida_da_chave_de_api(client: AsyncClient, container: Container) -> None:
    """Criar, usar com `X-API-Key`, rotacionar e revogar — e 401 depois de revogada.

    A chave completa aparece **uma unica vez** na criacao e outra na rotacao; o banco
    guarda apenas prefixo e hash bcrypt, entao nenhuma leitura posterior a reexibe.
    """
    await _cria_usuario(client, "admin@lukato.local", "admin")
    _liga_autenticacao(container)
    admin = _bearer(await _login(client, "admin@lukato.local"))

    criacao = await client.post(
        "/api/v1/identity/api-keys",
        json={"name": "integracao-billing", "role": "operator"},
        headers=admin,
    )
    assert criacao.status_code == 201, criacao.text
    chave = criacao.json()
    segredo_original = chave["secret"]
    assert segredo_original.startswith("lk_")
    assert chave["prefix"] in segredo_original

    usada = await client.get("/api/v1/identity/me", headers={"X-API-Key": segredo_original})
    assert usada.status_code == 200, usada.text
    assert usada.json()["kind"] == "api_key"
    assert usada.json()["role"] == "operator"

    rotacao = await client.post(f"/api/v1/identity/api-keys/{chave['id']}/rotate", headers=admin)
    assert rotacao.status_code == 200, rotacao.text
    segredo_novo = rotacao.json()["secret"]
    assert segredo_novo != segredo_original, "a rotacao precisa sortear um segredo novo"

    antiga = await client.get("/api/v1/identity/me", headers={"X-API-Key": segredo_original})
    nova = await client.get("/api/v1/identity/me", headers={"X-API-Key": segredo_novo})
    assert antiga.status_code == 401, "a credencial anterior morre na rotacao"
    assert nova.status_code == 200, nova.text

    revogacao = await client.delete(f"/api/v1/identity/api-keys/{chave['id']}", headers=admin)
    assert revogacao.status_code == 204

    depois = await client.get("/api/v1/identity/me", headers={"X-API-Key": segredo_novo})
    assert depois.status_code == 401, "chave revogada nao autentica mais"
    assert depois.json()["error"]["message"] == INVALID_CREDENTIALS


async def test_chave_de_api_com_segredo_errado_devolve_401(
    client: AsyncClient, container: Container
) -> None:
    """O prefixo apenas indexa a linha; quem autentica e o segredo."""
    criacao = await client.post(
        "/api/v1/identity/api-keys", json={"name": "integracao", "role": "operator"}
    )
    assert criacao.status_code == 201, criacao.text
    prefixo = criacao.json()["prefix"]
    _liga_autenticacao(container)

    resposta = await client.get(
        "/api/v1/identity/me", headers={"X-API-Key": f"lk_{prefixo}_segredo-errado"}
    )

    assert resposta.status_code == 401
    assert resposta.json()["error"]["message"] == INVALID_CREDENTIALS


async def test_chave_de_api_com_expiracao_ja_vencida_e_recusada_na_criacao(
    client: AsyncClient,
) -> None:
    """Uma chave que nasce vencida seria inutil: a criacao responde 422."""
    resposta = await client.post(
        "/api/v1/identity/api-keys",
        json={"name": "ja-vencida", "role": "operator", "expires_at": PASSADO.isoformat()},
    )

    assert resposta.status_code == 422
    assert resposta.json()["error"]["code"] == "validation_error"


async def test_listagem_de_chaves_mostra_o_prefixo_e_nunca_o_segredo(
    client: AsyncClient,
) -> None:
    """A leitura posterior traz apenas o prefixo publico (SPEC-0006 criterio 3)."""
    criacao = await client.post(
        "/api/v1/identity/api-keys", json={"name": "integracao", "role": "operator"}
    )
    assert criacao.status_code == 201, criacao.text
    segredo = criacao.json()["secret"]

    listagem = await client.get("/api/v1/identity/api-keys")

    assert listagem.status_code == 200, listagem.text
    item = listagem.json()["items"][0]
    assert item["prefix"] == criacao.json()["prefix"]
    assert "secret" not in item, "o segredo nao pode reaparecer em leitura"
    assert segredo not in listagem.text, "nem o texto cru da resposta pode conte-lo"
    _exige_sem_segredo("GET /identity/api-keys", listagem.json())


# --------------------------------------------------------------------------- #
# Varredura de segredos
# --------------------------------------------------------------------------- #
async def test_nenhuma_resposta_de_identidade_carrega_hash_de_senha_ou_de_chave(
    client: AsyncClient, container: Container, seeded: SeedIds
) -> None:
    """Varredura recursiva: nenhum nivel de nenhuma resposta traz campo sensivel.

    A garantia e do **schema** de saida, nao da disciplina de quem escreve a rota —
    mas so uma varredura por todo o JSON prova isso, porque um campo aninhado em
    `user`, em `items[]` ou em `details` escaparia de um assert por campo.
    """
    usuario = await _cria_usuario(client, "auditado@lukato.local", "operator")
    chave = await client.post(
        "/api/v1/identity/api-keys", json={"name": "auditoria", "role": "operator"}
    )
    assert chave.status_code == 201, chave.text

    respostas = {
        "POST /identity/users": usuario,
        "GET /identity/users": (await client.get("/api/v1/identity/users")).json(),
        "GET /identity/users/{id}": (
            await client.get(f"/api/v1/identity/users/{usuario['id']}")
        ).json(),
        "PUT /identity/users/{id}": (
            await client.put(f"/api/v1/identity/users/{usuario['id']}", json={"name": "Auditado"})
        ).json(),
        "POST /identity/api-keys": chave.json(),
        "GET /identity/api-keys": (await client.get("/api/v1/identity/api-keys")).json(),
        "POST /identity/api-keys/{id}/rotate": (
            await client.post(f"/api/v1/identity/api-keys/{chave.json()['id']}/rotate")
        ).json(),
        "GET /identity/me": (await client.get("/api/v1/identity/me")).json(),
        "POST /identity/login": (
            await client.post(
                "/api/v1/identity/login",
                json={"email": "auditado@lukato.local", "password": SENHA},
            )
        ).json(),
    }

    for rotulo, corpo in respostas.items():
        _exige_sem_segredo(rotulo, corpo)


async def test_erro_de_login_nao_vaza_campo_sensivel_nos_detalhes(
    client: AsyncClient,
) -> None:
    """Nem o envelope de erro carrega hash: `details` fica vazio de proposito."""
    await _cria_usuario(client, "alvo@lukato.local", "viewer")

    resposta = await client.post(
        "/api/v1/identity/login", json={"email": "alvo@lukato.local", "password": "errada-mesmo"}
    )

    assert resposta.status_code == 401
    _exige_sem_segredo("401 de login", resposta.json())
    assert resposta.json()["error"]["details"] == {}


async def test_troca_da_propria_senha_exige_a_senha_atual(
    client: AsyncClient, container: Container
) -> None:
    """Sem a confirmacao, um token roubado viraria posse permanente da conta."""
    await _cria_usuario(client, "dono@lukato.local", "operator")
    _liga_autenticacao(container)
    dono = _bearer(await _login(client, "dono@lukato.local"))

    sem_confirmacao = await client.post(
        "/api/v1/identity/users/me/password",
        json={"new_password": "outra-senha-forte"},
        headers=dono,
    )
    com_confirmacao = await client.post(
        "/api/v1/identity/users/me/password",
        json={"new_password": "outra-senha-forte", "current_password": SENHA},
        headers=dono,
    )

    assert sem_confirmacao.status_code == 401, sem_confirmacao.text
    assert sem_confirmacao.json()["error"]["message"] == INVALID_CREDENTIALS
    assert com_confirmacao.status_code == 200, com_confirmacao.text
    _exige_sem_segredo("POST /identity/users/me/password", com_confirmacao.json())

    novo = await client.post(
        "/api/v1/identity/login",
        json={"email": "dono@lukato.local", "password": "outra-senha-forte"},
    )
    assert novo.status_code == 200, "a senha nova precisa autenticar"
