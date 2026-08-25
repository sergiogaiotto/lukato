"""Ciclo de vida de um building block, ponta a ponta (SPEC-0001 secoes 3, 4 e 7).

Este arquivo e a prova do requisito central do lukato: **o nucleo nao conhece
modulo nenhum**. Um building block escrito fora da arvore `builtin/` e registrado
em tempo de execucao — exatamente o que um entry point faz no boot — e a partir
dai a plataforma inteira o trata como qualquer outro: ele aparece no registry,
ganha item de menu no console, recebe definicoes, executa pela trinca, e cada
execucao vira um `AgentRun` auditavel.

O roteiro, na ordem em que os testes o percorrem::

    registra a classe no registry
      -> duas ModuleDefinition sobre a MESMA classe, com trincas diferentes
      -> invoca as duas: o comportamento difere sem uma linha de codigo nova
      -> troca o guardrail de saida de uma delas: a mudanca vale na hora
      -> pausa a definicao: a invocacao seguinte responde 409
      -> toda invocacao deixou AgentRun com GUARDRAIL_IN -> PROMPT -> LLM ->
         GUARDRAIL_OUT e custo apurado
      -> provedor fora do ar: AgentRun FAILED persistido, nunca invisivel

`BlocoDeEnsaio` nao contem regra de negocio, de proposito. Se contivesse, a
diferenca de comportamento entre `triagem` e `resumo` poderia vir do codigo — e o
que se quer demonstrar e justamente que ela vem **so** do binding.
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

import pytest
from httpx import AsyncClient

from lukato.application.container import Container
from lukato.domain.models.module import ModuleKind
from lukato.domain.models.run import RunStatus, StepKind
from lukato.modules.base import (
    BaseModule,
    ModuleContext,
    ModuleRequest,
    ModuleResponse,
    UIDescriptor,
    UINavItem,
)
from lukato.modules.registry import ModuleRegistry
from tests.conftest import TEST_MODEL
from tests.fakes import FailingLLM

pytestmark = pytest.mark.integration

API: Final[str] = "/api/v1"
"""Prefixo global da API (SPEC-0000 secao 11)."""

SLUG_DA_CLASSE: Final[str] = "bloco-de-ensaio"
"""Slug da classe registrada em tempo de execucao (o *codigo*)."""

DEFINICAO_TRIAGEM: Final[str] = "triagem"
"""Primeira definicao sobre a classe: com guardrail de entrada que redige."""

DEFINICAO_RESUMO: Final[str] = "resumo"
"""Segunda definicao sobre a mesma classe: sem guardrail de entrada."""

PEDIDO: Final[str] = "preciso do relatorio confidencial da diretoria ate sexta"
"""Entrada usada nas duas definicoes — o que muda e a trinca, nunca o pedido."""

TERMO_SENSIVEL: Final[str] = "confidencial"
"""Termo que a politica de entrada da `triagem` redige e a de saida bloqueia."""

REDACAO: Final[str] = "[REDIGIDO]"
"""Marcador de redacao padrao (`Settings.guardrails.redaction_token`)."""

TRILHA_NORMATIVA: Final[tuple[str, ...]] = (
    StepKind.GUARDRAIL_IN.value,
    StepKind.PROMPT.value,
    StepKind.LLM.value,
    StepKind.GUARDRAIL_OUT.value,
)
"""Ordem exigida pela SPEC-0001 secao 4 para uma execucao bem-sucedida."""


# --------------------------------------------------------------------------- #
# O building block de fora da arvore
# --------------------------------------------------------------------------- #
class BlocoDeEnsaio(BaseModule):
    """Agente generico sem regra propria, registrado em tempo de execucao.

    Faz o minimo que um building block precisa fazer: pede a fachada da trinca
    (`ctx.services["pipeline"]`, montada pelo `InvokeModule`), executa uma
    chamada de LLM por ela e devolve o texto. Todo o comportamento observavel —
    o que e recusado na entrada, quem o agente e, o que e recusado na saida —
    chega pela `ModuleDefinition`.

    A classe **nao** usa `@register_module`: registra-la no import contaminaria
    o registry singleton de toda a suite. Quem a registra e o teste, que e
    tambem o ponto sendo provado (registro em tempo de execucao).
    """

    kind: ClassVar[ModuleKind] = ModuleKind.AGENT
    slug: ClassVar[str] = SLUG_DA_CLASSE
    name: ClassVar[str] = "Bloco de ensaio"
    description: ClassVar[str] = "Building block de terceiros, registrado sem tocar no nucleo."
    version: ClassVar[str] = "2.1.0"
    capabilities: ClassVar[tuple[str, ...]] = ("chat",)
    config_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"etiqueta": {"type": "string"}},
        "additionalProperties": True,
    }

    def __init__(self) -> None:
        """Zera os contadores usados para conferir o ciclo de vida."""
        self.setups = 0
        self.chamadas = 0

    async def setup(self, ctx: ModuleContext) -> None:
        """Conta as inicializacoes: `InvokeModule` deve chamar `setup` uma vez so."""
        self.setups += 1

    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Executa a etapa 8 pela fachada da trinca e devolve texto e rastro."""
        self.chamadas += 1
        pipeline = ctx.service("pipeline")
        resposta = await pipeline.complete(request.input)
        return ModuleResponse(
            output=resposta.content,
            data={
                "system_prompt": pipeline.system_prompt,
                "entrada_recebida": request.input,
                "modelo": resposta.model,
                "etiqueta": ctx.definition.config.get("etiqueta", ""),
            },
        )

    def ui(self) -> UIDescriptor:
        """Publica um item de menu — e assim que um modulo aparece no console."""
        return UIDescriptor(
            nav=[
                UINavItem(
                    label="Ensaio",
                    icon="blocks",
                    endpoint=f"/modules/{DEFINICAO_TRIAGEM}",
                    section="FUNCIONALIDADE",
                    order=90,
                )
            ]
        )


# --------------------------------------------------------------------------- #
# Montagem do cenario
# --------------------------------------------------------------------------- #
async def _criar_politica(
    client: AsyncClient, *, slug: str, stage: str, acao: str, termo: str
) -> str:
    """Cria uma politica de uma regra `keyword_block` e devolve o seu id."""
    resposta = await client.post(
        f"{API}/guardrails",
        json={
            "slug": slug,
            "name": slug,
            "stage": stage,
            "rules": [
                {
                    "id": "termo-sensivel",
                    "kind": "keyword_block",
                    "action": acao,
                    "severity": "high",
                    "config": {"keywords": [termo]},
                    "message": f"O termo '{termo}' nao passa no estagio {stage}.",
                }
            ],
        },
    )
    assert resposta.status_code == 201, resposta.text
    return str(resposta.json()["id"])


async def _criar_prompt(client: AsyncClient, *, slug: str, template: str) -> str:
    """Cria um system prompt e devolve o seu id."""
    resposta = await client.post(
        f"{API}/prompts", json={"slug": slug, "name": slug, "template": template}
    )
    assert resposta.status_code == 201, resposta.text
    return str(resposta.json()["id"])


async def _criar_definicao(
    client: AsyncClient,
    *,
    slug: str,
    nome: str,
    entrada: str | None,
    prompt: str,
    saida: str | None,
    etiqueta: str,
) -> dict[str, Any]:
    """Cria uma `ModuleDefinition` ativa sobre a classe registrada."""
    resposta = await client.post(
        f"{API}/modules",
        json={
            "slug": slug,
            "name": nome,
            "kind": ModuleKind.AGENT.value,
            "status": "active",
            "runtime": "direct",
            "binding": {
                "input_guardrail_id": entrada,
                "system_prompt_id": prompt,
                "output_guardrail_id": saida,
                "model": TEST_MODEL,
                "temperature": 0.0,
                "max_tokens": 256,
            },
            "config": {"module": SLUG_DA_CLASSE, "etiqueta": etiqueta},
        },
    )
    assert resposta.status_code == 201, resposta.text
    return dict(resposta.json())


@pytest.fixture
def bloco_registrado(container: Container) -> ModuleRegistry:
    """Registra `BlocoDeEnsaio` no registry do processo, em tempo de execucao.

    E o mesmo caminho de um entry point `lukato.modules`, so que sem instalar
    pacote: `registry.register(classe, source="entry_point")`.
    """
    antes = len(container.registry)
    container.registry.register(BlocoDeEnsaio, source="entry_point")
    assert len(container.registry) == antes + 1
    return container.registry


@pytest.fixture
async def cenario(client: AsyncClient, bloco_registrado: ModuleRegistry) -> dict[str, Any]:
    """Duas definicoes sobre a mesma classe, com trincas deliberadamente diferentes.

    `triagem` recebe um guardrail de entrada que **redige** o termo sensivel;
    `resumo` nao tem guardrail de entrada nenhum. As duas compartilham a politica
    de saida vazia (`None`) e diferem tambem no system prompt.
    """
    entrada = await _criar_politica(
        client,
        slug="ensaio-entrada-redige",
        stage="input",
        acao="redact",
        termo=TERMO_SENSIVEL,
    )
    saida_bloqueia = await _criar_politica(
        client,
        slug="ensaio-saida-bloqueia",
        stage="output",
        acao="block",
        termo=TERMO_SENSIVEL,
    )
    prompt_triagem = await _criar_prompt(
        client,
        slug="ensaio-triagem",
        template="Voce e o classificador de pedidos internos. Responda com a fila.",
    )
    prompt_resumo = await _criar_prompt(
        client,
        slug="ensaio-resumo",
        template="Voce e o resumidor executivo. Responda em uma frase.",
    )
    triagem = await _criar_definicao(
        client,
        slug=DEFINICAO_TRIAGEM,
        nome="Triagem de pedidos",
        entrada=entrada,
        prompt=prompt_triagem,
        saida=None,
        etiqueta="fila-interna",
    )
    resumo = await _criar_definicao(
        client,
        slug=DEFINICAO_RESUMO,
        nome="Resumo executivo",
        entrada=None,
        prompt=prompt_resumo,
        saida=None,
        etiqueta="diretoria",
    )
    return {
        "entrada": entrada,
        "saida_bloqueia": saida_bloqueia,
        "prompt_triagem": prompt_triagem,
        "prompt_resumo": prompt_resumo,
        "triagem": triagem,
        "resumo": resumo,
    }


async def _invocar(client: AsyncClient, slug: str, texto: str = PEDIDO) -> Any:
    """Invoca uma definicao pela API e devolve a resposta HTTP crua."""
    return await client.post(f"{API}/modules/{slug}/invoke", json={"input": texto})


async def _trilha(client: AsyncClient, run_id: str) -> list[dict[str, Any]]:
    """Passos persistidos de uma execucao, em ordem de indice."""
    resposta = await client.get(f"{API}/runs/{run_id}/steps")
    assert resposta.status_code == 200, resposta.text
    return list(resposta.json()["items"])


# --------------------------------------------------------------------------- #
# Registro em tempo de execucao (SPEC-0001 secoes 2 e 3)
# --------------------------------------------------------------------------- #
async def test_building_block_registrado_em_tempo_de_execucao_aparece_no_registry(
    client: AsyncClient, bloco_registrado: ModuleRegistry
) -> None:
    """`GET /api/v1/registry` lista o modulo novo com capacidades e schema."""
    resposta = await client.get(f"{API}/registry")

    assert resposta.status_code == 200
    descritores = {item["slug"]: item for item in resposta.json()["items"]}
    assert SLUG_DA_CLASSE in descritores, f"registry so tem {sorted(descritores)}"
    descritor = descritores[SLUG_DA_CLASSE]
    assert descritor["kind"] == ModuleKind.AGENT.value
    assert descritor["version"] == "2.1.0"
    assert descritor["capabilities"] == ["chat"]
    assert descritor["config_schema"]["properties"]["etiqueta"]["type"] == "string"
    assert descritor["source"] == "entry_point"


async def test_os_cinco_embutidos_continuam_no_registry_ao_lado_do_novo(
    client: AsyncClient, bloco_registrado: ModuleRegistry
) -> None:
    """Registrar um modulo de fora nao desloca nenhum dos embutidos (criterio 1)."""
    slugs = {item["slug"] for item in (await client.get(f"{API}/registry")).json()["items"]}

    assert {"auth", "processing", "finops", "knowledge", "adwatch"} <= slugs
    assert SLUG_DA_CLASSE in slugs


async def test_modulo_de_fora_publica_item_de_menu_no_console(
    client: AsyncClient, bloco_registrado: ModuleRegistry
) -> None:
    """O `UIDescriptor` do modulo entra na sidebar sem mudanca alguma no nucleo."""
    html = (await client.get("/modules")).text

    assert f'href="/modules/{DEFINICAO_TRIAGEM}"' in html
    assert '<span class="lk-nav__label">Ensaio</span>' in html, (
        "o item de menu publicado pelo modulo nao apareceu na sidebar"
    )
    assert 'aria-label="Ensaio"' in html, "o item de menu recolhido ficou sem rotulo acessivel"


async def test_remover_o_modulo_do_registry_nao_derruba_a_aplicacao(
    client: AsyncClient, bloco_registrado: ModuleRegistry
) -> None:
    """Desinstalar um building block e uma linha; o console segue de pe (criterio 4)."""
    bloco_registrado.unregister(SLUG_DA_CLASSE)

    assert (await client.get("/")).status_code == 200
    assert (await client.get(f"{API}/registry")).status_code == 200
    assert SLUG_DA_CLASSE not in bloco_registrado


# --------------------------------------------------------------------------- #
# Duas definicoes sobre a mesma classe (SPEC-0001 secao 7, criterio 2)
# --------------------------------------------------------------------------- #
async def test_duas_definicoes_apontam_para_a_mesma_classe(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """`triagem` e `resumo` sao configuracoes distintas do mesmo codigo."""
    assert cenario["triagem"]["id"] != cenario["resumo"]["id"]
    assert cenario["triagem"]["config"]["module"] == SLUG_DA_CLASSE
    assert cenario["resumo"]["config"]["module"] == SLUG_DA_CLASSE
    assert (
        cenario["triagem"]["binding"]["system_prompt_id"]
        != (cenario["resumo"]["binding"]["system_prompt_id"])
    )


async def test_a_trinca_diferente_produz_comportamento_diferente_sem_mudar_codigo(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """Mesma classe, mesmo pedido, saidas diferentes — a diferenca esta no binding.

    `triagem` tem guardrail de entrada que redige o termo sensivel antes de
    qualquer chamada ao provedor; `resumo` nao tem guardrail de entrada. O eco do
    provedor devolve o que recebeu, entao a redacao aparece — ou nao — na saida.
    """
    triagem = await _invocar(client, DEFINICAO_TRIAGEM)
    resumo = await _invocar(client, DEFINICAO_RESUMO)

    assert triagem.status_code == 200, triagem.text
    assert resumo.status_code == 200, resumo.text
    saida_triagem = triagem.json()["output"]
    saida_resumo = resumo.json()["output"]
    assert saida_triagem != saida_resumo, "as duas definicoes produziram a mesma saida"
    assert REDACAO in saida_triagem, f"a triagem nao redigiu o termo: {saida_triagem}"
    assert TERMO_SENSIVEL not in saida_triagem
    assert TERMO_SENSIVEL in saida_resumo, f"o resumo nao deveria redigir nada: {saida_resumo}"


async def test_cada_definicao_recebe_o_seu_proprio_system_prompt(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """O prompt renderizado na etapa 7 e o que a definicao vinculou, nao o da classe."""
    triagem = (await _invocar(client, DEFINICAO_TRIAGEM)).json()
    resumo = (await _invocar(client, DEFINICAO_RESUMO)).json()

    assert "classificador de pedidos internos" in triagem["data"]["system_prompt"]
    assert "resumidor executivo" in resumo["data"]["system_prompt"]
    assert triagem["data"]["etiqueta"] == "fila-interna"
    assert resumo["data"]["etiqueta"] == "diretoria"


async def test_a_mesma_instancia_da_classe_atende_as_duas_definicoes(
    client: AsyncClient, cenario: dict[str, Any], bloco_registrado: ModuleRegistry
) -> None:
    """O registry cacheia a instancia por slug de classe e `setup` roda uma vez so."""
    await _invocar(client, DEFINICAO_TRIAGEM)
    await _invocar(client, DEFINICAO_RESUMO)

    instancia = bloco_registrado.instantiate(SLUG_DA_CLASSE)
    assert isinstance(instancia, BlocoDeEnsaio)
    assert instancia.chamadas == 2, "as duas invocacoes deveriam usar a mesma instancia"
    assert instancia.setups == 1, f"`setup` rodou {instancia.setups} vezes"


# --------------------------------------------------------------------------- #
# Troca de guardrail de saida em tempo de execucao
# --------------------------------------------------------------------------- #
async def test_trocar_o_guardrail_de_saida_muda_o_resultado_na_hora(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """Sem reiniciar nada: a proxima invocacao ja obedece a politica nova."""
    antes = await _invocar(client, DEFINICAO_RESUMO)
    assert antes.status_code == 200, antes.text
    assert TERMO_SENSIVEL in antes.json()["output"]

    troca = await client.put(
        f"{API}/modules/{DEFINICAO_RESUMO}",
        json={
            "binding": {
                "input_guardrail_id": None,
                "system_prompt_id": cenario["prompt_resumo"],
                "output_guardrail_id": cenario["saida_bloqueia"],
                "model": TEST_MODEL,
                "temperature": 0.0,
                "max_tokens": 256,
            }
        },
    )
    assert troca.status_code == 200, troca.text

    depois = await _invocar(client, DEFINICAO_RESUMO)

    assert depois.status_code == 422, depois.text
    erro = depois.json()["error"]
    assert erro["code"] == "guardrail_violation"
    assert erro["details"]["findings"], "o bloqueio precisa listar o achado que o causou"
    assert erro["details"]["findings"][0]["rule_id"] == "termo-sensivel"


async def test_a_troca_de_guardrail_nao_afeta_a_outra_definicao(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """Definicao e configuracao **por linha**: mudar uma nao mexe na irma."""
    await client.put(
        f"{API}/modules/{DEFINICAO_RESUMO}",
        json={
            "binding": {
                "system_prompt_id": cenario["prompt_resumo"],
                "output_guardrail_id": cenario["saida_bloqueia"],
                "model": TEST_MODEL,
            }
        },
    )

    triagem = await _invocar(client, DEFINICAO_TRIAGEM)

    assert triagem.status_code == 200, triagem.text
    assert REDACAO in triagem.json()["output"]


async def test_bloqueio_de_saida_persiste_o_run_como_blocked(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """Execucao barrada tambem e execucao: fica gravada com `BLOCKED` e trilha."""
    await client.put(
        f"{API}/modules/{DEFINICAO_RESUMO}",
        json={
            "binding": {
                "system_prompt_id": cenario["prompt_resumo"],
                "output_guardrail_id": cenario["saida_bloqueia"],
                "model": TEST_MODEL,
            }
        },
    )
    await _invocar(client, DEFINICAO_RESUMO)

    pagina = (await client.get(f"{API}/runs", params={"module_slug": DEFINICAO_RESUMO})).json()

    assert pagina["total"] == 1
    execucao = pagina["items"][0]
    assert execucao["status"] == RunStatus.BLOCKED.value
    tipos = [passo["kind"] for passo in await _trilha(client, execucao["id"])]
    assert tipos == list(TRILHA_NORMATIVA), (
        f"a trilha da execucao bloqueada veio {tipos}; o bloqueio ocorre no ultimo passo"
    )


# --------------------------------------------------------------------------- #
# Ciclo de vida: pausar a definicao
# --------------------------------------------------------------------------- #
async def test_desativar_a_definicao_faz_a_invocacao_responder_409(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """Somente `active` pode ser invocado (SPEC-0001 secao 4, etapa 2)."""
    assert (await _invocar(client, DEFINICAO_TRIAGEM)).status_code == 200

    pausa = await client.patch(
        f"{API}/modules/{DEFINICAO_TRIAGEM}/status", json={"status": "paused"}
    )
    assert pausa.status_code == 200, pausa.text

    recusa = await _invocar(client, DEFINICAO_TRIAGEM)

    assert recusa.status_code == 409, recusa.text
    erro = recusa.json()["error"]
    assert erro["code"] == "conflict"
    assert erro["details"]["status"] == "paused"
    assert erro["details"]["required_status"] == "active"


async def test_definicao_pausada_nao_deixa_run_novo(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """A recusa acontece antes da etapa 5: nao nasce `AgentRun` para o que nao rodou."""
    await _invocar(client, DEFINICAO_TRIAGEM)
    await client.patch(f"{API}/modules/{DEFINICAO_TRIAGEM}/status", json={"status": "paused"})

    await _invocar(client, DEFINICAO_TRIAGEM)

    pagina = (await client.get(f"{API}/runs", params={"module_slug": DEFINICAO_TRIAGEM})).json()
    assert pagina["total"] == 1, "a invocacao recusada por status nao pode gerar execucao"


async def test_reativar_a_definicao_devolve_a_invocacao(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """Pausar e reversivel: voltar para `active` volta a permitir a invocacao."""
    await client.patch(f"{API}/modules/{DEFINICAO_TRIAGEM}/status", json={"status": "paused"})
    assert (await _invocar(client, DEFINICAO_TRIAGEM)).status_code == 409

    await client.patch(f"{API}/modules/{DEFINICAO_TRIAGEM}/status", json={"status": "active"})

    assert (await _invocar(client, DEFINICAO_TRIAGEM)).status_code == 200


# --------------------------------------------------------------------------- #
# Nao existe execucao invisivel (SPEC-0001 secao 7, criterio 5)
# --------------------------------------------------------------------------- #
async def test_toda_invocacao_deixa_um_agentrun_persistido(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """Tres invocacoes, tres execucoes gravadas, cada uma com o seu `run_id`."""
    respostas = [
        (await _invocar(client, DEFINICAO_TRIAGEM)).json(),
        (await _invocar(client, DEFINICAO_RESUMO)).json(),
        (await _invocar(client, DEFINICAO_TRIAGEM)).json(),
    ]

    identificadores = [item["run_id"] for item in respostas]
    assert all(identificadores), "alguma invocacao voltou sem run_id"
    assert len(set(identificadores)) == 3, "duas invocacoes reaproveitaram o mesmo run"
    for run_id in identificadores:
        assert (await client.get(f"{API}/runs/{run_id}")).status_code == 200


@pytest.mark.parametrize("slug", [DEFINICAO_TRIAGEM, DEFINICAO_RESUMO])
async def test_a_trilha_da_execucao_segue_a_ordem_normativa(
    client: AsyncClient, cenario: dict[str, Any], slug: str
) -> None:
    """`GUARDRAIL_IN -> PROMPT -> LLM -> GUARDRAIL_OUT`, nesta ordem, sempre.

    Vale inclusive para `resumo`, que **nao** tem guardrail de entrada: politica
    ausente e uma escolha registrada, nao um passo omitido (SPEC-0003 secao 1).
    """
    resposta = (await _invocar(client, slug)).json()

    passos = await _trilha(client, resposta["run_id"])
    assert [passo["kind"] for passo in passos] == list(TRILHA_NORMATIVA)
    assert [passo["index"] for passo in passos] == [0, 1, 2, 3]
    assert all(passo["status"] == RunStatus.SUCCEEDED.value for passo in passos)


async def test_a_execucao_registra_consumo_e_custo(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """O passo de LLM e precificado e o custo sobe ate o `AgentRun` (etapa 10)."""
    resposta = (await _invocar(client, DEFINICAO_TRIAGEM)).json()

    assert resposta["usage"]["total_tokens"] > 0
    assert resposta["cost_usd"] > 0.0, "a execucao saiu de graca com modelo precificado"
    execucao = (await client.get(f"{API}/runs/{resposta['run_id']}")).json()
    assert execucao["cost_usd"] == pytest.approx(resposta["cost_usd"])
    passos_llm = [
        passo for passo in await _trilha(client, resposta["run_id"]) if passo["kind"] == "llm"
    ]
    assert len(passos_llm) == 1
    assert passos_llm[0]["cost_usd"] > 0.0
    assert passos_llm[0]["usage"]["total_tokens"] > 0


async def test_o_consumo_da_execucao_vira_registro_de_finops(
    client: AsyncClient, cenario: dict[str, Any]
) -> None:
    """A etapa 10 grava `UsageRecord`: o custo aparece no resumo por modulo."""
    await _invocar(client, DEFINICAO_TRIAGEM)

    resumo = (await client.get(f"{API}/finops/summary")).json()

    assert resumo["by_module"].get(DEFINICAO_TRIAGEM, 0.0) > 0.0, resumo
    assert resumo["by_model"].get(TEST_MODEL, 0.0) > 0.0, resumo


# --------------------------------------------------------------------------- #
# Falha do runtime
# --------------------------------------------------------------------------- #
async def test_falha_do_provedor_persiste_o_agentrun_como_failed(
    client: AsyncClient, cenario: dict[str, Any], container: Container
) -> None:
    """Provedor fora do ar vira `502`, e a execucao fica gravada com `FAILED`.

    A troca do `LLMPort` por :class:`~tests.fakes.FailingLLM` simula a queda sem
    depender de rede: a fachada da trinca le `container.llm` no momento da
    invocacao.
    """
    container.llm = FailingLLM(message="hub de inferencia fora do ar")

    resposta = await _invocar(client, DEFINICAO_TRIAGEM)

    assert resposta.status_code == 502, resposta.text
    assert resposta.json()["error"]["code"] == "provider_error"
    pagina = (await client.get(f"{API}/runs", params={"module_slug": DEFINICAO_TRIAGEM})).json()
    assert pagina["total"] == 1, "a execucao que falhou nao foi persistida"
    assert pagina["items"][0]["status"] == RunStatus.FAILED.value


async def test_o_run_que_falhou_guarda_o_motivo_e_a_trilha_ate_o_ponto_da_queda(
    client: AsyncClient, cenario: dict[str, Any], container: Container
) -> None:
    """A trilha para onde a execucao parou e termina com um passo `ERROR`."""
    container.llm = FailingLLM(message="hub de inferencia fora do ar")
    await _invocar(client, DEFINICAO_TRIAGEM)

    execucao = (await client.get(f"{API}/runs", params={"module_slug": DEFINICAO_TRIAGEM})).json()[
        "items"
    ][0]
    passos = await _trilha(client, execucao["id"])

    tipos = [passo["kind"] for passo in passos]
    assert tipos == [
        StepKind.GUARDRAIL_IN.value,
        StepKind.PROMPT.value,
        StepKind.ERROR.value,
    ], f"a trilha da falha veio {tipos}"
    detalhe = (await client.get(f"{API}/runs/{execucao['id']}")).json()
    assert "hub de inferencia fora do ar" in (detalhe["error"] or "")
    assert passos[-1]["status"] == RunStatus.FAILED.value
    assert passos[-1]["error"]


async def test_a_falha_nao_impede_a_execucao_seguinte(
    client: AsyncClient, cenario: dict[str, Any], container: Container
) -> None:
    """Provedor de volta, plataforma de volta: nada fica preso ao erro anterior."""
    saudavel = container.llm
    container.llm = FailingLLM()
    assert (await _invocar(client, DEFINICAO_TRIAGEM)).status_code == 502

    container.llm = saudavel

    retomada = await _invocar(client, DEFINICAO_TRIAGEM)
    assert retomada.status_code == 200, retomada.text
    assert REDACAO in retomada.json()["output"]
