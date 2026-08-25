"""Testes de unidade dos adaptadores de seguranca (SPEC-0006 secoes 2, 3 e 4).

Tres contratos sao verificados aqui:

* `BcryptHasher` — o hash nunca e a senha, a verificacao distingue certo de errado,
  senha acima de 72 bytes **nao** e truncada silenciosamente (o pre-hash SHA-256
  preserva a entropia inteira) e hash malformado devolve `False` em vez de virar
  500 na rota de login.
* `JwtTokenService` — ida e volta do `Principal`, e recusa com `UnauthorizedError`
  para token expirado, assinatura de outro segredo e emissor diferente.
* `SlidingWindowRateLimiter` — a janela e deslizante e o relogio e injetado, entao o
  teste anda no tempo sem nenhum `sleep`.

O custo do bcrypt e fixado no piso (`rounds=4`) porque o que se testa e o contrato,
nao a lentidao proposital do algoritmo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import jwt
import pytest

from lukato.adapters.security import tokens as tokens_module
from lukato.adapters.security.cache import InMemoryCache, SlidingWindowRateLimiter
from lukato.adapters.security.hashing import MAX_ROUNDS, MIN_ROUNDS, BcryptHasher, prehash
from lukato.adapters.security.tokens import (
    API_KEY_NAMESPACE,
    ISSUER,
    JwtTokenService,
    generate_api_key,
    split_api_key,
)
from lukato.config.settings import Settings
from lukato.domain.errors import ConfigurationError, UnauthorizedError, ValidationError
from lukato.domain.models.identity import Permission, Principal, Role, permissions_for

pytestmark = pytest.mark.unit

SEGREDO = "segredo-de-teste-com-mais-de-32-caracteres"
"""Segredo HS256 usado por todos os testes de token deste arquivo."""

OUTRO_SEGREDO = "outro-segredo-de-teste-com-mais-de-32-chars"
"""Segredo diferente, para provar que a assinatura errada e recusada."""

PASSADO = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)
"""Instante fixo no passado: um token emitido nele ja nasce expirado."""


def _settings(**security: Any) -> Settings:
    """`Settings` de teste sem `.env`, com o grupo de seguranca do chamador."""
    base = {"auth_enabled": True, "jwt_secret": SEGREDO, "jwt_expires_seconds": 3600}
    return Settings(
        _env_file=None,
        llm={"provider": "echo"},
        embedding={"provider": "hashing"},
        security={**base, **security},
    )


@pytest.fixture
def hasher() -> BcryptHasher:
    """`BcryptHasher` no custo minimo: o teste mede contrato, nao segundos de CPU."""
    return BcryptHasher(rounds=MIN_ROUNDS)


@pytest.fixture
def servico() -> JwtTokenService:
    """`JwtTokenService` sobre o segredo de teste."""
    return JwtTokenService(_settings())


@pytest.fixture
def operador() -> Principal:
    """Principal de papel `operator`, com as permissoes do proprio papel."""
    return Principal(
        subject="usuario@lukato.teste",
        role=Role.OPERATOR,
        tenant_id="claro",
        kind="user",
        permissions=permissions_for(Role.OPERATOR),
    )


# --------------------------------------------------------------------------- #
# BcryptHasher
# --------------------------------------------------------------------------- #
def test_hash_nunca_e_a_propria_senha(hasher: BcryptHasher) -> None:
    senha = "s3nh@-do-usuario"

    resultado = hasher.hash(senha)

    assert resultado != senha
    assert senha not in resultado
    assert resultado.startswith("$2"), f"formato bcrypt esperado, veio {resultado[:6]!r}"


def test_verify_aceita_a_senha_certa_e_recusa_a_errada(hasher: BcryptHasher) -> None:
    resultado = hasher.hash("s3nh@-do-usuario")

    assert hasher.verify("s3nh@-do-usuario", resultado) is True
    assert hasher.verify("s3nh@-do-usuari0", resultado) is False


def test_dois_hashes_da_mesma_senha_diferem_pelo_sal(hasher: BcryptHasher) -> None:
    primeiro = hasher.hash("mesma-senha")
    segundo = hasher.hash("mesma-senha")

    assert primeiro != segundo, "cada hash usa um sal novo"
    assert hasher.verify("mesma-senha", primeiro) is True
    assert hasher.verify("mesma-senha", segundo) is True


def test_senha_acima_de_72_bytes_nao_e_truncada_silenciosamente(hasher: BcryptHasher) -> None:
    comum = "a" * 80
    divergente = comum[:72] + "DIFERENTE"

    assert len(comum.encode()) > 72
    assert comum[:72] == divergente[:72], "as duas senhas so diferem depois do byte 72"

    resultado = hasher.hash(comum)
    assert hasher.verify(comum, resultado) is True
    assert hasher.verify(divergente, resultado) is False, (
        "sem o pre-hash SHA-256 o bcrypt truncaria em 72 bytes e as duas senhas colidiriam"
    )


def test_prehash_reduz_qualquer_senha_a_64_bytes_ascii() -> None:
    for senha in ("", "curta", "z" * 5000, "acentuacao-com-cedilha-c"):
        reduzida = prehash(senha)
        assert len(reduzida) == 64
        assert reduzida.decode("ascii")


@pytest.mark.parametrize(
    "hash_ruim",
    ["", "   ", "nao-e-um-hash", "$2b$", "$2b$04$curto-demais", "hash-com-acento-cafe"],
)
def test_hash_malformado_devolve_false_sem_levantar(hasher: BcryptHasher, hash_ruim: str) -> None:
    assert hasher.verify("qualquer-senha", hash_ruim) is False


def test_custo_fora_da_faixa_suportada_e_recusado() -> None:
    with pytest.raises(ValidationError):
        BcryptHasher(rounds=MIN_ROUNDS - 1)
    with pytest.raises(ValidationError):
        BcryptHasher(rounds=MAX_ROUNDS + 1)


def test_needs_rehash_reconhece_hash_de_outro_custo(hasher: BcryptHasher) -> None:
    resultado = hasher.hash("senha")

    assert hasher.needs_rehash(resultado) is False
    assert BcryptHasher(rounds=MIN_ROUNDS + 1).needs_rehash(resultado) is True
    assert hasher.needs_rehash("lixo") is True


# --------------------------------------------------------------------------- #
# JwtTokenService
# --------------------------------------------------------------------------- #
def test_token_faz_ida_e_volta_preservando_o_principal(
    servico: JwtTokenService, operador: Principal
) -> None:
    token = servico.issue(operador, expires_in=60)

    recuperado = servico.decode(token)

    assert recuperado.subject == operador.subject
    assert recuperado.role is Role.OPERATOR
    assert recuperado.tenant_id == "claro"
    assert recuperado.kind == "user"


def test_permissoes_sao_reconstruidas_do_papel_e_nao_lidas_do_token(
    servico: JwtTokenService, operador: Principal
) -> None:
    token = servico.issue(operador, expires_in=60)
    claims = jwt.decode(token, SEGREDO, algorithms=["HS256"], issuer=ISSUER)

    assert "permissions" not in claims, "o token nao pode carregar a lista de permissoes"
    assert servico.decode(token).permissions == permissions_for(Role.OPERATOR)
    assert servico.decode(token).can(Permission.MODULE_INVOKE) is True


def test_token_expirado_vira_unauthorized_error(
    servico: JwtTokenService, operador: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tokens_module, "utcnow", lambda: PASSADO)
    token = servico.issue(operador, expires_in=60)

    with pytest.raises(UnauthorizedError) as capturado:
        servico.decode(token)

    assert capturado.value.details["reason"] == "expired"
    assert capturado.value.http_status == 401


def test_token_assinado_com_outro_segredo_vira_unauthorized_error(
    servico: JwtTokenService, operador: Principal
) -> None:
    intruso = JwtTokenService(_settings(jwt_secret=OUTRO_SEGREDO))
    token = intruso.issue(operador, expires_in=60)

    with pytest.raises(UnauthorizedError) as capturado:
        servico.decode(token)

    assert capturado.value.details["reason"] == "invalid_token"


def test_token_de_outro_emissor_vira_unauthorized_error(servico: JwtTokenService) -> None:
    agora = int(datetime.now(tz=UTC).timestamp())
    token = jwt.encode(
        {
            "sub": "usuario@lukato.teste",
            "role": Role.OPERATOR.value,
            "tenant": "claro",
            "kind": "user",
            "iat": agora,
            "exp": agora + 600,
            "iss": "outro-servico",
        },
        SEGREDO,
        algorithm="HS256",
    )

    with pytest.raises(UnauthorizedError) as capturado:
        servico.decode(token)

    assert capturado.value.details["reason"] == "invalid_issuer"


def test_token_vazio_ou_com_papel_desconhecido_vira_unauthorized_error(
    servico: JwtTokenService,
) -> None:
    agora = int(datetime.now(tz=UTC).timestamp())
    papel_invalido = jwt.encode(
        {
            "sub": "alguem",
            "role": "imperador",
            "iat": agora,
            "exp": agora + 600,
            "iss": ISSUER,
        },
        SEGREDO,
        algorithm="HS256",
    )

    with pytest.raises(UnauthorizedError):
        servico.decode("")
    with pytest.raises(UnauthorizedError) as capturado:
        servico.decode(papel_invalido)
    assert capturado.value.details["reason"] == "unknown_role"


def test_servico_recusa_subir_com_segredo_vazio() -> None:
    with pytest.raises(ConfigurationError):
        JwtTokenService(_settings(jwt_secret="   "))


def test_issue_default_usa_a_validade_configurada(operador: Principal) -> None:
    servico = JwtTokenService(_settings(jwt_expires_seconds=120))

    claims = jwt.decode(
        servico.issue_default(operador), SEGREDO, algorithms=["HS256"], issuer=ISSUER
    )

    assert claims["exp"] - claims["iat"] == 120
    assert servico.expires_seconds == 120


# --------------------------------------------------------------------------- #
# Chaves de API
# --------------------------------------------------------------------------- #
def test_generate_api_key_produz_o_formato_lk_prefixo_segredo() -> None:
    completa, prefixo, segredo = generate_api_key()

    assert completa == f"{API_KEY_NAMESPACE}_{prefixo}_{segredo}"
    assert len(prefixo) == 8
    assert prefixo.isalnum() and prefixo.islower()
    assert len(segredo) >= 32, "o segredo vem de secrets.token_urlsafe(32)"


def test_duas_chaves_geradas_nunca_coincidem() -> None:
    primeira, _, _ = generate_api_key()
    segunda, _, _ = generate_api_key()

    assert primeira != segunda


def test_split_api_key_reverte_a_chave_gerada() -> None:
    completa, prefixo, segredo = generate_api_key()

    assert split_api_key(completa) == (prefixo, segredo)


def test_split_api_key_preserva_underscores_do_segredo() -> None:
    assert split_api_key("lk_abcd1234_seg_re_do") == ("abcd1234", "seg_re_do")


@pytest.mark.parametrize(
    "invalida",
    [
        "",
        "   ",
        "lk_semsegredo",
        "xx_abcd1234_segredo",
        "lk__segredo",
        "lk_PREFIXO_segredo",
        "lk_" + "a" * 600 + "_segredo",
    ],
)
def test_split_api_key_recusa_formatos_invalidos(invalida: str) -> None:
    assert split_api_key(invalida) is None


# --------------------------------------------------------------------------- #
# Rate limiter por janela deslizante
# --------------------------------------------------------------------------- #
class _RelogioControlado:
    """Relogio monotonico falso: o teste avanca o tempo sem esperar nada."""

    def __init__(self, inicio: float = 1000.0) -> None:
        self.agora = inicio

    def __call__(self) -> float:
        """Instante atual em segundos (interface de `time.monotonic`)."""
        return self.agora

    def avancar(self, segundos: float) -> None:
        """Move o relogio para a frente."""
        self.agora += segundos


async def test_rate_limiter_libera_ate_o_limite_e_barra_a_chamada_seguinte() -> None:
    relogio = _RelogioControlado()
    limitador = SlidingWindowRateLimiter(clock=relogio)

    resultados = [await limitador.allow("cliente-1", 3, 60.0) for _ in range(4)]

    assert [permitida for permitida, _ in resultados] == [True, True, True, False]
    assert [restantes for _, restantes in resultados] == [2, 1, 0, 0]


async def test_rate_limiter_volta_a_liberar_quando_a_janela_desliza() -> None:
    relogio = _RelogioControlado()
    limitador = SlidingWindowRateLimiter(clock=relogio)

    for _ in range(2):
        await limitador.allow("cliente-2", 2, 10.0)
    bloqueada, _ = await limitador.allow("cliente-2", 2, 10.0)

    relogio.avancar(11.0)
    liberada, restantes = await limitador.allow("cliente-2", 2, 10.0)

    assert bloqueada is False
    assert liberada is True, "passada a janela de 10s, as chamadas antigas nao contam mais"
    assert restantes == 1


async def test_rate_limiter_isola_as_chaves_entre_si() -> None:
    limitador = SlidingWindowRateLimiter(clock=_RelogioControlado())

    await limitador.allow("cliente-a", 1, 60.0)
    permitida_a, _ = await limitador.allow("cliente-a", 1, 60.0)
    permitida_b, _ = await limitador.allow("cliente-b", 1, 60.0)

    assert permitida_a is False
    assert permitida_b is True, "o limite de um cliente nao pode punir o outro"


async def test_rate_limiter_remaining_nao_consome_cota() -> None:
    limitador = SlidingWindowRateLimiter(clock=_RelogioControlado())
    await limitador.allow("cliente-3", 5, 60.0)

    antes = await limitador.remaining("cliente-3", 5, 60.0)
    depois = await limitador.remaining("cliente-3", 5, 60.0)

    assert antes == depois == 4


async def test_rate_limiter_reset_zera_o_historico_da_chave() -> None:
    limitador = SlidingWindowRateLimiter(clock=_RelogioControlado())
    await limitador.allow("cliente-4", 1, 60.0)

    await limitador.reset("cliente-4")

    permitida, _ = await limitador.allow("cliente-4", 1, 60.0)
    assert permitida is True


async def test_rate_limiter_recusa_janela_nao_positiva() -> None:
    limitador = SlidingWindowRateLimiter(clock=_RelogioControlado())

    with pytest.raises(ValidationError):
        await limitador.allow("cliente-5", 1, 0.0)


async def test_cache_expira_a_entrada_pelo_ttl_do_relogio_injetado() -> None:
    relogio = _RelogioControlado()
    cache = InMemoryCache(clock=relogio)

    await cache.set("chave", "valor", ttl_seconds=5.0)
    presente = await cache.get("chave")
    relogio.avancar(6.0)
    ausente = await cache.get("chave")

    assert presente == "valor"
    assert ausente is None


async def test_cache_despeja_a_entrada_mais_antiga_ao_estourar_o_teto() -> None:
    cache = InMemoryCache(max_entries=2, clock=_RelogioControlado())

    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.set("c", 3)

    assert await cache.get("a") is None, "a entrada mais antiga sai primeiro (FIFO)"
    assert await cache.get("b") == 2
    assert await cache.get("c") == 3
    assert await cache.size() == 2
