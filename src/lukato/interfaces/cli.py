"""Linha de comando do lukato (`lukato ...`), construida so com `argparse`.

Sete comandos, todos apoiados nos mesmos casos de uso que a API HTTP usa — a CLI
nao tem atalho para o banco nem regra propria:

```text
lukato serve      sobe a API + console com uvicorn
lukato seed       popula prompts, guardrails, modulos e o catalogo de demonstracao
lukato openapi    exporta o contrato OpenAPI 3.1 para um arquivo
lukato health     imprime o relatorio de prontidao em JSON
lukato modules    list | show | invoke — operacoes rapidas de building block
lukato adwatch    detect — roda o funil de deteccao sobre uma midia
lukato version    imprime a versao do pacote
```

**Log e saida sao coisas diferentes.** O `configure_logging` do projeto escreve em
`sys.stdout`; se o log ficasse em INFO, cada `lukato health | jq` viria com dezenas
de linhas de boot misturadas ao JSON. Por isso a CLI configura o log em `WARNING`
por padrao e so sobe para o nivel de `Settings` com `--verbose` (ou no `serve`, que
e um processo de servidor e nao uma consulta).

O `seed` e **idempotente**: rodar duas vezes nao duplica nada e nao falha. Ele
existe para que uma instalacao recem-criada ja tenha a trinca configurada, dois
agentes diferentes sobre a mesma classe `processing` e um caso de AdWatch pronto
para detectar.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from lukato import __version__
from lukato.adapters.guardrails.policies import default_policies
from lukato.application.container import Container
from lukato.application.dto import ModuleCreateInput, ModuleFilter
from lukato.application.use_cases.adwatch import (
    CommercialFilter,
    CommercialInput,
    CreateCommercial,
    DeleteCommercial,
    DeleteMedia,
    DetectCommercials,
    DetectionReport,
    GetCommercialByCode,
    GetMedia,
    ImportTranscript,
    ListCommercials,
    ListMedia,
    MediaFilter,
    MediaInput,
    RegisterMedia,
)
from lukato.application.use_cases.guardrails import (
    CreatePolicy,
    DeletePolicy,
    GetPolicyBySlug,
    ListPolicies,
    PolicyCreateInput,
    PolicyFilter,
)
from lukato.application.use_cases.health import GetProviderDetails, GetReadiness
from lukato.application.use_cases.identity import CreateUser, GetUser, UserCreateInput
from lukato.application.use_cases.modules import (
    CreateModule,
    DeleteModule,
    GetModule,
    InvokeModule,
    ListModules,
)
from lukato.application.use_cases.prompts import (
    CreatePrompt,
    DeletePrompt,
    GetPromptBySlug,
    ListPrompts,
    PromptCreateInput,
    PromptFilter,
)
from lukato.composition import build_container, dispose_container
from lukato.config import Settings, configure_logging, get_logger, get_settings
from lukato.domain.errors import ConflictError, LukatoError, NotFoundError
from lukato.domain.models.adwatch import MediaKind, TranscriptWord
from lukato.domain.models.guardrail import GuardrailPolicy
from lukato.domain.models.identity import Principal, Role
from lukato.domain.models.module import ModuleBinding, ModuleKind, ModuleStatus
from lukato.domain.models.prompt import PromptTemplate
from lukato.domain.types import Json
from lukato.modules.base import ModuleRequest

__all__ = [
    "EXIT_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "SEED_COMMERCIALS",
    "SEED_MEDIA_TITLE",
    "SEED_MEDIA_URI",
    "SEED_MODULES",
    "SEED_MODULE_SLUGS",
    "SEED_PROMPTS",
    "SEED_TRANSCRIPT",
    "SeedModule",
    "SeedPrompt",
    "build_parser",
    "main",
]

_logger = get_logger(__name__)

EXIT_OK: Final[int] = 0
"""Comando concluido com sucesso."""

EXIT_ERROR: Final[int] = 1
"""Erro de dominio, de configuracao ou instalacao nao pronta."""

EXIT_INTERRUPTED: Final[int] = 130
"""Interrompido pelo operador (Ctrl-C), pela convencao 128 + SIGINT."""

QUIET_LOG_LEVEL: Final[str] = "WARNING"
"""Nivel de log padrao da CLI: mantem `stdout` limpo para `jq` e para pipes."""

ROOT_EMAIL_ENV: Final[str] = "LUKATO_SEED_ROOT_EMAIL"
"""Variavel opcional com o e-mail do usuario root criado pelo seed."""

ROOT_PASSWORD_ENV: Final[str] = "LUKATO_SEED_ROOT_PASSWORD"  # noqa: S105 - nome de variavel
"""Variavel opcional com a senha do root; ausente, uma senha forte e sorteada.

A senha **nunca** vem do codigo: ou o operador a fornece por ambiente, ou o seed
sorteia uma com `secrets` e a imprime uma unica vez, para ser trocada no primeiro
acesso.
"""

DEFAULT_ROOT_EMAIL: Final[str] = "root@lukato.local"
"""E-mail do root de demonstracao quando o ambiente nao informa outro."""

GENERATED_PASSWORD_BYTES: Final[int] = 18
"""Entropia da senha sorteada para o root (24 caracteres em base64 url-safe)."""


# ---------------------------------------------------------------------------
# Saida
# ---------------------------------------------------------------------------
def _out(text: str = "") -> None:
    """Escreve uma linha em `stdout` (a CLI nao usa `print`)."""
    sys.stdout.write(f"{text}\n")


def _err(text: str) -> None:
    """Escreve uma linha em `stderr`, para diagnostico que nao e resultado."""
    sys.stderr.write(f"{text}\n")


def _json(payload: Any) -> None:
    """Escreve um documento JSON identado e sem escapar acentos."""
    _out(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Catalogo do seed
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SeedPrompt:
    """System prompt entregue com a instalacao."""

    slug: str
    name: str
    description: str
    template: str


SEED_PROMPTS: Final[tuple[SeedPrompt, ...]] = (
    SeedPrompt(
        slug="assistente-geral",
        name="Assistente geral",
        description="Conversa aberta, em portugues do Brasil, sem inventar informacao.",
        template=(
            "Voce e o assistente geral do lukato. Responda em portugues do Brasil, "
            "com objetividade e sem inventar informacao. Quando nao souber, diga "
            "que nao sabe e indique exatamente qual dado esta faltando."
        ),
    ),
    SeedPrompt(
        slug="triagem-atendimento",
        name="Triagem de atendimento",
        description="Classifica o pedido do cliente e indica a proxima acao.",
        template=(
            "Voce e o analista de triagem de um atendimento. Leia a mensagem do "
            "cliente e responda com: (1) a categoria do pedido, (2) a urgencia "
            "entre baixa, media e alta e (3) a proxima acao recomendada. Nao "
            "prometa prazo, nao cite valores e nao peca dados pessoais."
        ),
    ),
    SeedPrompt(
        slug="extrator-json",
        name="Extrator JSON",
        description="Saida estruturada, para uso com a politica de guardrail saida-json.",
        template=(
            "Voce e um extrator de dados. Leia o texto recebido e responda SOMENTE "
            "com um objeto JSON valido, sem cercas de codigo e sem texto antes ou "
            "depois. Use exatamente as chaves assunto, entidades, sentimento e "
            "resumo. Campo sem valor recebe null."
        ),
    ),
)
"""Os tres system prompts de seed; nenhum exige variavel, entao invocam de imediato."""


@dataclass(frozen=True, slots=True)
class SeedModule:
    """Definicao de modulo entregue com a instalacao.

    `implementation` e a **classe** do building block e `slug` e a **definicao**.
    `assistente` e `triagem` compartilham a classe `processing` e diferem apenas
    no binding: e essa dupla que prova o requisito central da SPEC-0001 secao 5.
    """

    slug: str
    name: str
    description: str
    implementation: str
    kind: ModuleKind
    runtime: str
    input_policy: str
    prompt: str
    output_policy: str
    temperature: float
    max_tokens: int
    timeout_seconds: float
    tools: tuple[str, ...]
    config: Json
    tags: tuple[str, ...]


SEED_MODULES: Final[tuple[SeedModule, ...]] = (
    SeedModule(
        slug="assistente",
        name="Assistente geral",
        description=(
            "Agente de conversa aberta sobre a classe 'processing'. Mesma classe da "
            "'triagem'; o que muda e o binding."
        ),
        implementation="processing",
        kind=ModuleKind.AGENT,
        runtime="langgraph",
        input_policy="entrada-padrao",
        prompt="assistente-geral",
        output_policy="saida-padrao",
        temperature=0.3,
        max_tokens=1024,
        timeout_seconds=60.0,
        tools=("now", "calculator", "knowledge_search"),
        config={"module": "processing", "planning": True, "max_iterations": 4},
        tags=("seed", "demo", "agente"),
    ),
    SeedModule(
        slug="triagem",
        name="Triagem de atendimento",
        description=(
            "Mesma classe 'processing' do assistente, com guardrail de entrada "
            "estrito, prompt de triagem, guardrail de saida auditado, temperatura "
            "zero e sem ferramentas. Nenhuma linha de codigo separa os dois."
        ),
        implementation="processing",
        kind=ModuleKind.AGENT,
        runtime="direct",
        input_policy="entrada-estrita",
        prompt="triagem-atendimento",
        output_policy="saida-auditada",
        temperature=0.0,
        max_tokens=512,
        timeout_seconds=30.0,
        tools=(),
        config={"module": "processing", "planning": False, "max_iterations": 1},
        tags=("seed", "demo", "atendimento"),
    ),
    SeedModule(
        slug="conhecimento",
        name="Base de conhecimento",
        description="Ingestao, chunking, embeddings e busca semantica sobre a colecao padrao.",
        implementation="knowledge",
        kind=ModuleKind.KNOWLEDGE,
        runtime="direct",
        input_policy="entrada-padrao",
        prompt="assistente-geral",
        output_policy="saida-padrao",
        temperature=0.1,
        max_tokens=1024,
        timeout_seconds=90.0,
        tools=(),
        config={"module": "knowledge", "search_limit": 5},
        tags=("seed", "demo", "conhecimento"),
    ),
    SeedModule(
        slug="adwatch",
        name="AdWatch",
        description=(
            "Catalogo de comerciais e deteccao temporal multimodal. O prompt "
            "'extrator-json' e o que o juiz visual recebe quando ha VLM disponivel."
        ),
        implementation="adwatch",
        kind=ModuleKind.PIPELINE,
        runtime="direct",
        input_policy="entrada-padrao",
        prompt="extrator-json",
        output_policy="saida-padrao",
        temperature=0.0,
        max_tokens=2048,
        timeout_seconds=300.0,
        tools=("commercial_lookup",),
        config={"module": "adwatch"},
        tags=("seed", "demo", "adwatch"),
    ),
)
"""As quatro definicoes de modulo do seed (duas delas sobre a mesma classe)."""

SEED_MODULE_SLUGS: Final[tuple[str, ...]] = tuple(item.slug for item in SEED_MODULES)
"""Slugs das definicoes criadas pelo seed, usados tambem pelo `--reset`."""

SEED_COMMERCIALS: Final[tuple[Json, ...]] = (
    {
        "commercial_id": "COM_000234",
        "campaign": "Pos 100GB",
        "brand": "Claro",
        "text": "Na Claro voce tem muito mais internet para aproveitar tudo que gosta",
        "keywords": ["Claro", "internet"],
        "key_phrases": ["muito mais internet"],
        "duration_expected": 30.0,
    },
    {
        "commercial_id": "COM_000235",
        "campaign": "Claro Flex",
        "brand": "Claro",
        "text": "Com Claro Flex voce controla tudo pelo aplicativo quando quiser",
        "keywords": ["Claro Flex", "aplicativo"],
        "key_phrases": ["controla tudo pelo aplicativo"],
        "duration_expected": 15.0,
    },
    {
        "commercial_id": "COM_000236",
        "campaign": "Plano Familia",
        "brand": "Claro",
        "text": "Conheca o novo plano Claro com 50 giga de internet e WhatsApp ilimitado",
        "keywords": ["50 giga", "WhatsApp"],
        "key_phrases": ["WhatsApp ilimitado"],
        "duration_expected": 30.0,
    },
    {
        "commercial_id": "COM_000237",
        "campaign": "Claro TV mais",
        "brand": "Claro",
        "text": "Assine o Claro TV e curta os melhores canais direto na sua televisao",
        "keywords": ["Claro TV", "canais"],
        "key_phrases": ["melhores canais"],
        "duration_expected": 20.0,
    },
    {
        "commercial_id": "COM_000238",
        "campaign": "Claro Fibra",
        "brand": "Claro",
        "text": "Claro fibra chega na sua casa com instalacao rapida e wifi de verdade",
        "keywords": ["Claro fibra", "wifi"],
        "key_phrases": ["instalacao rapida"],
        "duration_expected": 30.0,
    },
)
"""Cinco comerciais de exemplo; `CreateCommercial` gera a assinatura de cada um."""

SEED_MEDIA_URI: Final[str] = "file:///demo/programa-lukato.mp4"
"""URI do ativo de midia de demonstracao (o arquivo nao precisa existir em disco)."""

SEED_MEDIA_TITLE: Final[str] = "Programa demonstrativo lukato"
"""Titulo do ativo de midia criado pelo seed."""

SEED_TRANSCRIPT: Final[tuple[tuple[str, float, float], ...]] = (
    ("bem vindos ao programa de hoje vamos falar sobre tecnologia e mercado", 0.0, 60.0),
    ("nosso convidado explica como as operadoras estao investindo em rede", 60.0, 120.0),
    # COM_000234 falado com variacao lexical proposital ("mais" no lugar de "muito
    # mais", "pra" no lugar de "para"): o funil precisa achar o comercial mesmo
    # quando o locutor nao le o roteiro palavra por palavra.
    ("na claro voce tem mais internet pra aproveitar tudo o que voce gosta", 120.0, 150.0),
    ("voltamos agora com a segunda parte da nossa conversa sobre conectividade", 150.0, 240.0),
    ("encerramos por aqui e ate a proxima semana com mais novidades", 240.0, 300.0),
)
"""Roteiro sintetico da midia de demonstracao, com um comercial do catalogo dentro."""


def _transcript_words(
    script: Sequence[tuple[str, float, float]] = SEED_TRANSCRIPT,
) -> list[TranscriptWord]:
    """Distribui as palavras do roteiro no tempo, como faria o alinhamento do WhisperX."""
    words: list[TranscriptWord] = []
    for text, start, end in script:
        tokens = text.split()
        step = (end - start) / max(len(tokens), 1)
        for index, token in enumerate(tokens):
            words.append(
                TranscriptWord(
                    word=token,
                    start=start + index * step,
                    end=start + (index + 1) * step,
                )
            )
    return words


# ---------------------------------------------------------------------------
# Ciclo de vida da CLI
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _container_scope(settings: Settings) -> AsyncIterator[Container]:
    """Monta o container pelo composition root e garante o descarte no fim."""
    container, engine = await build_container(settings)
    try:
        yield container
    finally:
        await dispose_container(container, engine)


def _root() -> Principal:
    """Principal usado pela CLI: root local.

    Quem executa a CLI ja tem acesso ao banco e a configuracao do processo; exigir
    token seria teatro de seguranca. A autorizacao real acontece na borda HTTP.
    """
    return Principal.anonymous_root()


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------
async def _seed_policies(container: Container, principal: Principal) -> dict[str, GuardrailPolicy]:
    """Garante as cinco politicas de `default_policies()` e devolve `slug -> politica`."""
    stored: dict[str, GuardrailPolicy] = {}
    for policy in default_policies():
        try:
            created = await CreatePolicy(container).execute(
                PolicyCreateInput(
                    slug=policy.slug,
                    name=policy.name,
                    description=policy.description,
                    stage=policy.stage,
                    rules=list(policy.rules),
                    fail_open=policy.fail_open,
                    is_active=policy.is_active,
                ),
                principal,
            )
            _out(
                f"  guardrail  + {created.slug:<18} {created.stage.value:<7} "
                f"{len(created.rules)} regra(s)"
            )
        except ConflictError:
            created = await GetPolicyBySlug(container).execute(policy.slug, principal)
            _out(f"  guardrail  = {created.slug:<18} ja existia")
        stored[created.slug] = created
    return stored


async def _seed_prompts(container: Container, principal: Principal) -> dict[str, PromptTemplate]:
    """Garante os tres system prompts de seed e devolve `slug -> prompt`."""
    stored: dict[str, PromptTemplate] = {}
    for prompt in SEED_PROMPTS:
        try:
            created = await CreatePrompt(container).execute(
                PromptCreateInput(
                    slug=prompt.slug,
                    name=prompt.name,
                    description=prompt.description,
                    template=prompt.template,
                    labels=("seed",),
                ),
                principal,
            )
            _out(f"  prompt     + {created.slug:<18} v{created.version}")
        except ConflictError:
            created = await GetPromptBySlug(container).execute(prompt.slug, principal)
            _out(f"  prompt     = {created.slug:<18} ja existia (v{created.version})")
        stored[created.slug] = created
    return stored


async def _seed_modules(
    container: Container,
    principal: Principal,
    *,
    policies: dict[str, GuardrailPolicy],
    prompts: dict[str, PromptTemplate],
) -> int:
    """Garante as quatro definicoes de modulo e devolve quantas foram criadas agora."""
    settings = container.settings
    created_count = 0
    for item in SEED_MODULES:
        binding = ModuleBinding(
            input_guardrail_id=policies[item.input_policy].id,
            system_prompt_id=prompts[item.prompt].id,
            output_guardrail_id=policies[item.output_policy].id,
            model=settings.llm.model,
            temperature=item.temperature,
            max_tokens=item.max_tokens,
            timeout_seconds=item.timeout_seconds,
            tools=list(item.tools),
        )
        try:
            created = await CreateModule(container).execute(
                ModuleCreateInput(
                    slug=item.slug,
                    name=item.name,
                    description=item.description,
                    kind=item.kind,
                    status=ModuleStatus.ACTIVE,
                    runtime=item.runtime,
                    binding=binding,
                    config=dict(item.config),
                    tags=list(item.tags),
                    owner="seed",
                ),
                principal,
            )
            created_count += 1
            _out(
                f"  modulo     + {created.slug:<18} classe={item.implementation:<11} "
                f"runtime={created.runtime:<10} "
                f"trinca={item.input_policy}/{item.prompt}/{item.output_policy}"
            )
        except ConflictError:
            _out(f"  modulo     = {item.slug:<18} ja existia")
    return created_count


async def _seed_commercials(container: Container, principal: Principal) -> int:
    """Garante os cinco comerciais do catalogo (com assinatura) e devolve quantos entraram."""
    created_count = 0
    for payload in SEED_COMMERCIALS:
        code = str(payload["commercial_id"])
        try:
            created = await CreateCommercial(container).execute(
                CommercialInput(**payload), principal
            )
            created_count += 1
            _out(
                f"  comercial  + {created.commercial_id:<12} {created.campaign:<16} "
                f"{created.duration_expected:g}s"
            )
        except ConflictError:
            _out(f"  comercial  = {code:<12} ja existia")
    return created_count


async def _seed_media(container: Container, principal: Principal) -> str:
    """Garante a midia de demonstracao com transcricao sintetica e devolve o seu id.

    A transcricao entra pelo caminho de importacao, e nao pela ingestao: sem
    FFmpeg, sem WhisperX e sem GPU, `lukato adwatch detect` funciona logo depois
    do seed (SPEC-0010 secao 3.1).
    """
    existing = await ListMedia(container).execute(
        MediaFilter(search=SEED_MEDIA_URI, limit=5), principal
    )
    asset = next((item for item in existing.items if item.uri == SEED_MEDIA_URI), None)
    if asset is None:
        asset = await RegisterMedia(container).execute(
            MediaInput(
                uri=SEED_MEDIA_URI,
                kind=MediaKind.VIDEO,
                title=SEED_MEDIA_TITLE,
                duration_seconds=SEED_TRANSCRIPT[-1][2],
                fps=25.0,
                metadata={"origin": "seed", "synthetic": True},
            ),
            principal,
        )
        _out(f"  midia      + {asset.uri}")
    else:
        _out(f"  midia      = {asset.uri} ja existia")

    words = _transcript_words()
    transcript = await ImportTranscript(container).execute(
        asset.id, words, principal, language="pt", source="seed"
    )
    _out(
        f"  transcricao= {len(transcript.words)} palavras, "
        f"{SEED_TRANSCRIPT[-1][2]:g}s (contem COM_000234)"
    )
    return asset.id


async def _seed_semantic_notice(container: Container) -> None:
    """Avisa quando as assinaturas nasceram sem vetor semantico.

    `BuildFingerprint` degrada em silencio quando o provedor de embeddings nao
    responde: a assinatura e gravada sem vetor e o `semantic_match` de toda
    deteccao vale zero. Como o peso semantico e 0.25 e o teto sem OCR e sem juiz
    visual e 0.45, **nenhum** candidato alcanca o limiar de revisao (0.60) — o
    `detect` roda inteiro e devolve zero deteccoes. Sem este aviso, quem acabou de
    rodar o seed concluiria que o funil esta quebrado.
    """
    try:
        if await container.embeddings.health():
            return
    except Exception as exc:  # a sonda e informativa: falhar nela nao derruba o seed
        _logger.warning("seed_embedding_probe_failed", error=f"{type(exc).__name__}: {exc}")
    _out("  atencao    ! o provedor de embeddings nao respondeu e as assinaturas dos")
    _out("               comerciais ficaram sem vetor semantico; a deteccao tende a")
    _out("               ficar abaixo do limiar de revisao. Para o modo offline")
    _out("               completo, rode:")
    _out("                 LUKATO_EMBEDDING__PROVIDER=hashing lukato seed --reset")


async def _seed_root_user(container: Container, principal: Principal) -> None:
    """Cria o usuario root quando `LUKATO_SECURITY__AUTH_ENABLED` esta ligado.

    Com a autenticacao desligada (padrao em desenvolvimento) toda rota ja responde
    como root anonimo, e criar um usuario seria ruido. Com ela ligada, sem este
    usuario ninguem consegue o primeiro token.
    """
    if not container.settings.security.auth_enabled:
        _out("  usuario    . autenticacao desligada: nenhum usuario root criado")
        return

    email = (os.environ.get(ROOT_EMAIL_ENV) or DEFAULT_ROOT_EMAIL).strip()
    # A existencia e conferida **antes** de olhar a senha: `CreateUser` valida o
    # tamanho da senha antes de consultar o banco, e um seed repetido com uma
    # senha curta no ambiente falharia mesmo com o usuario ja criado.
    try:
        existing = await GetUser(container).execute(email, principal)
    except NotFoundError:
        existing = None
    if existing is not None:
        _out(f"  usuario    = {existing.email} ja existia (papel {existing.role.value})")
        return

    supplied = os.environ.get(ROOT_PASSWORD_ENV) or ""
    password = supplied or secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)
    try:
        user = await CreateUser(container).execute(
            UserCreateInput(
                email=email,
                password=password,
                name="Root",
                role=Role.ROOT,
                tenant_id="default",
            ),
            principal,
        )
    except ConflictError:  # corrida entre dois seeds simultaneos
        _out(f"  usuario    = {email} ja existia")
        return
    _out(f"  usuario    + {user.email} (papel {user.role.value})")
    if supplied:
        _out(f"               senha lida de {ROOT_PASSWORD_ENV}")
    else:
        _out(f"               senha sorteada: {password}")
        _out("               anote agora — ela nao sera exibida de novo — e troque no 1o acesso")


async def _reset_seed(container: Container, principal: Principal) -> None:
    """Apaga o que o seed cria, na ordem que respeita as dependencias.

    Usuarios **nao** entram no reset: apagar o root deixaria uma instalacao com
    autenticacao ligada sem ninguem para entrar nela.
    """
    _out("  reset      . removendo dados de seed anteriores")
    media = await ListMedia(container).execute(MediaFilter(limit=200), principal)
    for asset in media.items:
        if asset.uri == SEED_MEDIA_URI:
            await DeleteMedia(container).execute(asset.id, principal)
            _out(f"  reset      - midia {asset.uri} (deteccoes em cascata)")

    for payload in SEED_COMMERCIALS:
        code = str(payload["commercial_id"])
        try:
            found = await GetCommercialByCode(container).execute(code, principal)
        except NotFoundError:
            continue
        await DeleteCommercial(container).execute(found.id, principal)
        _out(f"  reset      - comercial {code}")

    for slug in SEED_MODULE_SLUGS:
        try:
            await DeleteModule(container).execute(slug, principal)
        except NotFoundError:
            continue
        _out(f"  reset      - modulo {slug}")

    for prompt in SEED_PROMPTS:
        try:
            await DeletePrompt(container).execute(prompt.slug, principal, all_versions=True)
        except NotFoundError:
            continue
        _out(f"  reset      - prompt {prompt.slug}")

    for policy in default_policies():
        try:
            await DeletePolicy(container).execute(policy.slug, principal)
        except NotFoundError:
            continue
        _out(f"  reset      - guardrail {policy.slug}")


async def _run_seed(settings: Settings, *, reset: bool) -> int:
    """Executa o seed completo e devolve o codigo de saida."""
    principal = _root()
    async with _container_scope(settings) as container:
        _out("seed do lukato — idempotente, pode rodar quantas vezes quiser")
        if reset:
            await _reset_seed(container, principal)
        policies = await _seed_policies(container, principal)
        prompts = await _seed_prompts(container, principal)
        await _seed_modules(container, principal, policies=policies, prompts=prompts)
        await _seed_commercials(container, principal)
        media_id = await _seed_media(container, principal)
        await _seed_semantic_notice(container)
        await _seed_root_user(container, principal)
        _out()
        _out("pronto. proximos passos:")
        _out("  lukato modules list")
        _out('  lukato modules invoke assistente --input "o que voce faz?"')
        _out('  lukato modules invoke triagem --input "minha internet caiu de novo"')
        _out(f"  lukato adwatch detect --media {media_id}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
LIMITE_EXPORT: Final[int] = 200
"""Teto por pagina das listagens. O export pagina ate acabar; o teto so evita
uma unica consulta gigante contra o banco."""


async def _todos(executar, filtro_de) -> list[Json]:
    """Percorre uma listagem paginada ate o fim e devolve os itens crus."""
    itens: list[Json] = []
    offset = 0
    while True:
        pagina = await executar(filtro_de(LIMITE_EXPORT, offset))
        lote = list(pagina.items)
        itens.extend(item.model_dump(mode="json") for item in lote)
        if len(lote) < LIMITE_EXPORT or len(itens) >= pagina.total:
            return itens
        offset += LIMITE_EXPORT


async def _montar_export(settings: Settings) -> Json:
    """Escreve num JSO o que esta instalacao tem de configuracao e catalogo.

    Existe porque um banco de demonstracao morre com o ambiente que o hospeda, e
    o que foi montado ali — prompts, politicas, modulos, catalogo de comerciais,
    base de conhecimento — e trabalho que se quer consultar depois, em outra
    maquina. `seed` planta o minimo; `export` leva o que VOCE montou.

    O que NAO sai daqui, de proposito:

    * segredo de chave de API — o valor so existe no instante em que a chave e
      criada ou rotacionada, e um arquivo de export circula por e-mail, anexo e
      repositorio. Exportar segredo seria transformar um arquivo de conveniencia
      numa via de vazamento;
    * hash de senha de usuario, pelo mesmo motivo;
    * execucoes e deteccoes — sao DERIVADAS. Reimportar uma deteccao criaria uma
      evidencia que nunca foi calculada naquela instalacao, que e o oposto do que
      a trilha de auditoria significa. Elas voltam rodando o funil de novo.
    """
    principal = _root()
    async with _container_scope(settings) as container:
        prompts = await _todos(
            lambda f: ListPrompts(container).execute(f, principal),
            lambda limite, salto: PromptFilter(limit=limite, offset=salto),
        )
        policies = await _todos(
            lambda f: ListPolicies(container).execute(f, principal),
            lambda limite, salto: PolicyFilter(limit=limite, offset=salto),
        )
        modules = await _todos(
            lambda f: ListModules(container).execute(f, principal),
            lambda limite, salto: ModuleFilter(limit=limite, offset=salto),
        )
        commercials = await _todos(
            lambda f: ListCommercials(container).execute(f, principal),
            lambda limite, salto: CommercialFilter(limit=limite, offset=salto),
        )
        media = await _todos(
            lambda f: ListMedia(container).execute(f, principal),
            lambda limite, salto: MediaFilter(limit=limite, offset=salto),
        )

    documento: Json = {
        "lukato_export": 1,
        "versao_da_aplicacao": __version__,
        "prompts": prompts,
        "guardrails": policies,
        "modules": modules,
        "commercials": commercials,
        "media": media,
        "nao_exportado": {
            "segredos": "chave de API e hash de senha nunca saem daqui",
            "derivados": "execucoes e deteccoes voltam rodando o funil de novo",
        },
    }
    return documento


def _run_import(settings: Settings, *, origem: str) -> int:
    """Le o arquivo fora do laco e aplica dentro dele — ver `_run_export`."""
    dados = json.loads(Path(origem).read_text(encoding="utf-8"))
    if dados.get("lukato_export") != 1:
        raise ValueError(f"{origem} nao parece um export do lukato (falta `lukato_export: 1`)")
    return asyncio.run(_aplicar_import(settings, dados))


def _run_export(settings: Settings, *, destino: str | None) -> int:
    """Monta o documento no laco e grava FORA dele.

    Escrever arquivo dentro de corotina bloqueia o laco de eventos. Numa CLI de
    um tiro so isso nao doi, mas a regra existe para o dia em que este mesmo
    codigo for chamado de dentro de um servidor — e ai doeria.
    """
    documento = asyncio.run(_montar_export(settings))
    texto = json.dumps(documento, ensure_ascii=False, indent=2, default=str)
    if destino:
        Path(destino).write_text(texto + "\n", encoding="utf-8")
        _out(f"export gravado em {destino}")
        for chave in ("prompts", "guardrails", "modules", "commercials", "media"):
            _out(f"  {chave:<13} {len(documento[chave])}")
    else:
        print(texto)
    return EXIT_OK


async def _aplicar_import(settings: Settings, dados: Json) -> int:
    """Recria numa instalacao o que `export` levou de outra.

    Idempotente pelo mesmo criterio do `seed`: o que ja existe pelo identificador
    (slug ou codigo do comercial) e mantido, nao sobrescrito. Rodar duas vezes o
    mesmo arquivo nao duplica nada e nao apaga nada.

    Midia NAO e recriada: `uri` aponta para um caminho da maquina de origem, e
    registrar aqui um caminho que nao existe cria um ativo que nenhuma etapa
    consegue ler. A tela de AdWatch registra a midia em tres campos — e o
    caminho e a unica coisa que so quem esta na maquina sabe.

    HISTORICO DE VERSAO NAO VIAJA. O export lista as versoes de prompt que
    existem na origem; a importacao cria a PRIMEIRA versao de cada slug, com o
    texto da versao mais recente. Um export de 18 linhas de prompt sobre 13 slugs
    vira 13 prompts em v1. Fingir o contrario seria escrever no destino uma
    trilha de auditoria que nunca aconteceu ali.
    """
    principal = _root()
    contagem = {"criado": 0, "ja existia": 0}

    def marcar(rotulo: str, nome: str, novo: bool) -> None:
        chave = "criado" if novo else "ja existia"
        contagem[chave] += 1
        _out(f"  {rotulo:<12} {'+' if novo else '='} {nome}")

    async with _container_scope(settings) as container:
        for item in dados.get("prompts", []):
            try:
                await CreatePrompt(container).execute(
                    PromptCreateInput(
                        slug=item["slug"],
                        name=item.get("name", ""),
                        description=item.get("description", ""),
                        role=item.get("role", "system"),
                        template=item.get("template", ""),
                        labels=item.get("labels", []),
                        is_active=item.get("is_active", True),
                    ),
                    principal,
                )
                marcar("prompt", item["slug"], True)
            except ConflictError:
                marcar("prompt", item["slug"], False)

        for item in dados.get("guardrails", []):
            try:
                await CreatePolicy(container).execute(
                    PolicyCreateInput(
                        slug=item["slug"],
                        name=item.get("name", ""),
                        description=item.get("description", ""),
                        stage=item.get("stage", "input"),
                        rules=item.get("rules", []),
                        fail_open=item.get("fail_open", False),
                        is_active=item.get("is_active", True),
                    ),
                    principal,
                )
                marcar("guardrail", item["slug"], True)
            except ConflictError:
                marcar("guardrail", item["slug"], False)

        for item in dados.get("modules", []):
            try:
                await CreateModule(container).execute(
                    ModuleCreateInput(
                        slug=item["slug"],
                        name=item.get("name", ""),
                        description=item.get("description", ""),
                        kind=item.get("kind", "agent"),
                        status=item.get("status", "active"),
                        runtime=item.get("runtime", "direct"),
                        config=item.get("config", {}),
                        tags=item.get("tags", []),
                    ),
                    principal,
                )
                marcar("modulo", item["slug"], True)
            except ConflictError:
                marcar("modulo", item["slug"], False)

        for item in dados.get("commercials", []):
            codigo = item["commercial_id"]
            try:
                await CreateCommercial(container).execute(
                    CommercialInput(
                        commercial_id=codigo,
                        campaign=item.get("campaign", ""),
                        brand=item.get("brand", ""),
                        text=item.get("text", ""),
                        duration_expected=item.get("duration_expected", 30.0),
                        keywords=item.get("keywords", []),
                        key_phrases=item.get("key_phrases", []),
                        language=item.get("language", "pt-BR"),
                        is_active=item.get("is_active", True),
                    ),
                    principal,
                )
                marcar("comercial", codigo, True)
            except ConflictError:
                marcar("comercial", codigo, False)

    _out()
    _out(f"{contagem['criado']} criados, {contagem['ja existia']} ja existiam")
    slugs = {item["slug"] for item in dados.get("prompts", [])}
    if len(dados.get("prompts", [])) > len(slugs):
        _out(
            f"{len(dados['prompts'])} linhas de prompt sobre {len(slugs)} slugs: cada slug "
            "chega em v1. Historico de versao nao viaja entre instalacoes."
        )
    if dados.get("media"):
        _out(
            f"{len(dados['media'])} midia(s) NAO foram recriadas: o caminho do arquivo e da "
            "maquina de origem. Registre a sua em /adwatch."
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
async def _run_health(settings: Settings) -> int:
    """Imprime prontidao e detalhe de provedores em JSON; `down` devolve `1`."""
    async with _container_scope(settings) as container:
        readiness = await GetReadiness(container).execute()
        providers = await GetProviderDetails(container).execute()
    report = readiness.to_dict()
    report["providers"] = providers.to_dict()["providers"]
    _json(report)
    return EXIT_OK if readiness.ready else EXIT_ERROR


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------
async def _run_modules_list(settings: Settings, *, limit: int) -> int:
    """Lista as definicoes de modulo em uma tabela curta."""
    principal = _root()
    async with _container_scope(settings) as container:
        page = await ListModules(container).execute(ModuleFilter(limit=limit), principal)
    if not page.items:
        _out("nenhuma definicao de modulo cadastrada — rode 'lukato seed'")
        return EXIT_OK
    _out(f"{'SLUG':<18} {'KIND':<10} {'STATUS':<10} {'RUNTIME':<10} NOME")
    for definition in page.items:
        _out(
            f"{definition.slug:<18} {definition.kind.value:<10} "
            f"{definition.status.value:<10} {definition.runtime:<10} {definition.name}"
        )
    _out(f"\n{page.total} definicao(oes)")
    return EXIT_OK


async def _run_modules_show(settings: Settings, *, reference: str) -> int:
    """Imprime a definicao completa de um modulo em JSON."""
    principal = _root()
    async with _container_scope(settings) as container:
        definition = await GetModule(container).execute(reference, principal)
    _json(definition.model_dump(mode="json"))
    return EXIT_OK


def _parse_variables(pairs: Sequence[str]) -> Json:
    """Converte `--var chave=valor` repetido em um dicionario."""
    variables: Json = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"variavel invalida: '{pair}'; use o formato chave=valor")
        variables[key.strip()] = value
    return variables


async def _run_modules_invoke(settings: Settings, *, slug: str, text: str, variables: Json) -> int:
    """Invoca um modulo pela trinca completa e imprime a resposta em JSON."""
    principal = _root()
    async with _container_scope(settings) as container:
        response = await InvokeModule(container).execute(
            slug, ModuleRequest(input=text, variables=variables), principal
        )
    _json(
        {
            "run_id": response.run_id,
            "output": response.output,
            "data": response.data,
            "usage": response.usage.model_dump(mode="json"),
            "cost_usd": response.cost_usd,
            "findings": [finding.model_dump(mode="json") for finding in response.findings],
            "metadata": response.metadata,
        }
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# adwatch
# ---------------------------------------------------------------------------
async def _resolve_media(container: Container, principal: Principal, reference: str) -> str:
    """Resolve a midia por identificador, por URI ou — sem referencia — pela mais recente."""
    candidate = (reference or "").strip()
    if candidate:
        try:
            return (await GetMedia(container).execute(candidate, principal)).id
        except NotFoundError:
            page = await ListMedia(container).execute(
                MediaFilter(search=candidate, limit=10), principal
            )
            for asset in page.items:
                if candidate in (asset.uri, asset.title):
                    return asset.id
            raise

    page = await ListMedia(container).execute(MediaFilter(limit=1), principal)
    if not page.items:
        raise NotFoundError(
            "Nenhum ativo de midia cadastrado; rode 'lukato seed' ou informe --media.",
            details={"media_id": ""},
        )
    return page.items[0].id


def _print_detections(report: DetectionReport) -> None:
    """Imprime o resumo legivel de um relatorio de deteccao."""
    if not report.detections:
        _out("nenhuma deteccao acima do limiar nesta midia")
    else:
        _out(f"{'COMERCIAL':<12} {'INICIO':>9} {'FIM':>9} {'CONF':>6}  STATUS")
        for detection in report.detections:
            _out(
                f"{detection.commercial_code:<12} {detection.start:>9.1f} "
                f"{detection.end:>9.1f} {detection.confidence:>6.3f}  {detection.status.value}"
            )
    _out()
    _out(
        f"janelas={report.windows} candidatos={report.candidates} "
        f"comerciais={report.commercials} persistidas={report.persisted} "
        f"substituidas={report.replaced}"
    )
    _out(
        f"aceitas={report.accepted} revisao={report.needs_review} "
        f"rejeitadas={report.rejected} vlm={'sim' if report.vision_available else 'nao'} "
        f"semantico={'sim' if report.semantic_enabled else 'nao'} "
        f"tempo={report.elapsed_ms:.0f}ms"
    )


async def _run_adwatch_detect(
    settings: Settings, *, media: str, keep_rejected: bool, as_json: bool
) -> int:
    """Roda o funil de deteccao sobre uma midia e imprime o relatorio."""
    principal = _root()
    async with _container_scope(settings) as container:
        media_id = await _resolve_media(container, principal, media)
        report = await DetectCommercials(container).execute(
            media_id, principal, keep_rejected=keep_rejected
        )
    if as_json:
        _json(report.to_dict())
    else:
        _print_detections(report)
    return EXIT_OK


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------
def _run_serve(settings: Settings, *, host: str, port: int, reload: bool) -> int:
    """Sobe a aplicacao com uvicorn apontando para `lukato.main:app`."""
    import uvicorn

    _logger.info(
        "cli_serve",
        host=host,
        port=port,
        reload=reload,
        workers=1 if reload else settings.app.workers,
        environment=settings.app.env,
    )
    uvicorn.run(
        "lukato.main:app",
        host=host,
        port=port,
        reload=reload,
        # `--reload` e `--workers` sao mutuamente exclusivos no uvicorn: o
        # recarregador precisa ser o unico dono do processo filho.
        workers=None if reload else settings.app.workers,
        log_level=settings.observability.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# openapi
# ---------------------------------------------------------------------------
def _run_openapi(settings: Settings, *, out: str) -> int:
    """Exporta o contrato OpenAPI 3.1 sem subir a aplicacao."""
    from lukato.interfaces.http.openapi import export_openapi
    from lukato.main import create_app

    app = create_app(settings)
    export_openapi(app, out)
    document = app.openapi()
    _out(
        f"contrato exportado para {out} "
        f"({len(document.get('paths', {}))} caminho(s), versao {document['info']['version']})"
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser(settings: Settings) -> argparse.ArgumentParser:
    """Monta o parser completo da CLI, com os defaults vindos de `Settings`."""
    parser = argparse.ArgumentParser(
        prog="lukato",
        description="lukato — ecossistema modular de agentes de IA.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="eleva o log ao nivel de LUKATO_OBSERVABILITY__LOG_LEVEL (padrao: WARNING)",
    )
    parser.add_argument("--version", action="version", version=f"lukato {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMANDO")

    serve = commands.add_parser("serve", help="sobe a API e o console com uvicorn")
    serve.add_argument("--host", default=settings.app.host, help="endereco de bind")
    serve.add_argument("--port", type=int, default=settings.app.port, help="porta TCP")
    serve.add_argument(
        "--reload", action="store_true", help="recarrega ao salvar (apenas desenvolvimento)"
    )
    serve.set_defaults(handler="serve")

    seed = commands.add_parser(
        "seed", help="popula prompts, guardrails, modulos e o catalogo de demonstracao"
    )
    seed.add_argument(
        "--reset",
        action="store_true",
        help="remove os dados de seed anteriores antes de recria-los (nao apaga usuarios)",
    )
    seed.set_defaults(handler="seed")

    openapi = commands.add_parser("openapi", help="exporta o contrato OpenAPI 3.1")
    openapi.add_argument("--out", required=True, metavar="CAMINHO", help="arquivo de destino")
    openapi.set_defaults(handler="openapi")

    export = commands.add_parser(
        "export",
        help="grava em JSON os prompts, guardrails, modulos, comerciais e midias desta instalacao",
    )
    export.add_argument(
        "--out",
        metavar="CAMINHO",
        help="arquivo de destino; sem ele o JSON sai na saida padrao",
    )
    export.set_defaults(handler="export")

    importar = commands.add_parser(
        "import", help="recria nesta instalacao o que um `lukato export` levou de outra"
    )
    importar.add_argument("arquivo", metavar="ARQUIVO", help="JSON gerado por `lukato export`")
    importar.set_defaults(handler="import")

    health = commands.add_parser("health", help="imprime o relatorio de prontidao em JSON")
    health.set_defaults(handler="health")

    modules = commands.add_parser("modules", help="operacoes rapidas de building block")
    module_commands = modules.add_subparsers(dest="subcommand", required=True, metavar="OPERACAO")

    modules_list = module_commands.add_parser("list", help="lista as definicoes cadastradas")
    modules_list.add_argument("--limit", type=int, default=50, help="quantas definicoes listar")
    modules_list.set_defaults(handler="modules.list")

    modules_show = module_commands.add_parser("show", help="mostra uma definicao em JSON")
    modules_show.add_argument("reference", metavar="SLUG", help="slug ou identificador")
    modules_show.set_defaults(handler="modules.show")

    modules_invoke = module_commands.add_parser("invoke", help="invoca um modulo pela trinca")
    modules_invoke.add_argument("slug", metavar="SLUG", help="slug da definicao")
    modules_invoke.add_argument("--input", default="", help="texto de entrada")
    modules_invoke.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="CHAVE=VALOR",
        help="variavel do system prompt (pode repetir)",
    )
    modules_invoke.set_defaults(handler="modules.invoke")

    adwatch = commands.add_parser("adwatch", help="operacoes do AdWatch")
    adwatch_commands = adwatch.add_subparsers(dest="subcommand", required=True, metavar="OPERACAO")
    detect = adwatch_commands.add_parser("detect", help="roda o funil de deteccao sobre uma midia")
    detect.add_argument(
        "--media",
        default="",
        metavar="ID",
        help="identificador ou URI da midia (padrao: a midia mais recente)",
    )
    detect.add_argument(
        "--keep-rejected", action="store_true", help="mantem tambem os candidatos rejeitados"
    )
    detect.add_argument("--json", action="store_true", help="imprime o relatorio completo em JSON")
    detect.set_defaults(handler="adwatch.detect")

    version = commands.add_parser("version", help="imprime a versao do pacote")
    version.set_defaults(handler="version")

    return parser


# ---------------------------------------------------------------------------
# Despacho
# ---------------------------------------------------------------------------
def _dispatch(args: argparse.Namespace, settings: Settings) -> int:
    """Executa o comando escolhido e devolve o codigo de saida."""
    handler = args.handler
    if handler == "version":
        _out(__version__)
        return EXIT_OK
    if handler == "serve":
        return _run_serve(settings, host=args.host, port=args.port, reload=args.reload)
    if handler == "openapi":
        return _run_openapi(settings, out=args.out)
    if handler == "seed":
        return asyncio.run(_run_seed(settings, reset=args.reset))
    if handler == "export":
        return _run_export(settings, destino=args.out)
    if handler == "import":
        return _run_import(settings, origem=args.arquivo)
    if handler == "health":
        return asyncio.run(_run_health(settings))
    if handler == "modules.list":
        return asyncio.run(_run_modules_list(settings, limit=args.limit))
    if handler == "modules.show":
        return asyncio.run(_run_modules_show(settings, reference=args.reference))
    if handler == "modules.invoke":
        variables = _parse_variables(args.var)
        return asyncio.run(
            _run_modules_invoke(settings, slug=args.slug, text=args.input, variables=variables)
        )
    if handler == "adwatch.detect":
        return asyncio.run(
            _run_adwatch_detect(
                settings, media=args.media, keep_rejected=args.keep_rejected, as_json=args.json
            )
        )
    raise ValueError(f"comando sem tratamento: '{handler}'")


def main(argv: Sequence[str] | None = None) -> int:
    """Ponto de entrada do console script `lukato`; devolve o codigo de saida."""
    settings = get_settings()
    parser = build_parser(settings)
    args = parser.parse_args(list(argv) if argv is not None else None)

    level = settings.observability.log_level if args.verbose else QUIET_LOG_LEVEL
    if args.handler == "serve":
        level = settings.observability.log_level
    configure_logging(
        level=level, json_logs=settings.observability.log_json, service=settings.app.name
    )

    try:
        return _dispatch(args, settings)
    except KeyboardInterrupt:
        _err("interrompido")
        return EXIT_INTERRUPTED
    except LukatoError as exc:
        _err(json.dumps({"error": exc.to_dict()}, ensure_ascii=False, default=str))
        return EXIT_ERROR
    except ValueError as exc:
        _err(f"erro: {exc}")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - execucao por `python -m`
    raise SystemExit(main())
