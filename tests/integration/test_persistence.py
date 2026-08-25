"""Testes de integracao da persistencia (SPEC-0011, criterios de aceite da secao 10).

Exercitam os **doze repositorios** e a unidade de trabalho contra um SQLite/aiosqlite
em memoria, o mesmo caminho de codigo que roda em PostgreSQL. Cobrem:

* round-trip completo de cada agregado — criar, ler por id, ler pela chave natural,
  listar com cada filtro, contar, atualizar e apagar;
* a fronteira do adaptador: nenhum repositorio devolve linha ORM (SPEC-0011 secao 3.8);
* conflitos de chave natural (`ConflictError`) e ausencia (`NotFoundError`);
* as cascatas `ON DELETE CASCADE`, que so funcionam porque o engine vem de
  `build_engine` e liga `PRAGMA foreign_keys=ON` (SPEC-0011 secao 9);
* idempotencia de `save_scenes`/`save_ocr`/`save_transcript`/`upsert_fingerprint`;
* a semantica transacional do `SqlAlchemyUnitOfWork`.

Tudo e deterministico: os objetos vem de `tests.factories`, cujos identificadores sao
UUIDv5 derivados da chave natural e cujos carimbos saem da data fixa `AGORA`.
"""

from __future__ import annotations

import pytest

from lukato.adapters.persistence import orm
from lukato.adapters.persistence.uow import UnitOfWorkFactoryImpl
from lukato.domain.errors import ConfigurationError, ConflictError, NotFoundError
from lukato.domain.models.adwatch import (
    AdFingerprint,
    Commercial,
    Detection,
    DetectionStatus,
    MediaAsset,
    OcrText,
    SceneCut,
    Transcript,
)
from lukato.domain.models.finops import Budget, UsageRecord
from lukato.domain.models.guardrail import GuardrailPolicy, GuardrailStage
from lukato.domain.models.identity import ApiKey, Role, User
from lukato.domain.models.knowledge import Chunk, Document
from lukato.domain.models.module import ModuleDefinition, ModuleKind, ModuleStatus
from lukato.domain.models.prompt import PromptTemplate
from lukato.domain.models.run import AgentRun, RunStatus, RunStep, TokenUsage
from tests.factories import (
    AGORA,
    id_de,
    make_api_key,
    make_budget,
    make_chunk,
    make_commercial,
    make_detection,
    make_document,
    make_fingerprint,
    make_media,
    make_module,
    make_ocr,
    make_policy,
    make_prompt,
    make_run,
    make_scenes,
    make_step,
    make_transcript,
    make_usage_record,
    make_user,
    momento,
)

pytestmark = pytest.mark.integration

ID_AUSENTE = id_de("id", "que", "nunca", "foi", "gravado")
"""Identificador valido que nunca e inserido — alvo dos testes de `NotFoundError`."""


def prova_que_e_dominio(objeto: object, modelo: type, linha: type) -> None:
    """Afirma que `objeto` e o modelo de dominio e **nao** a linha ORM correspondente."""
    assert isinstance(objeto, modelo), (
        f"esperava um {modelo.__name__} do dominio, veio {type(objeto).__name__}"
    )
    assert not isinstance(objeto, linha), (
        f"vazamento de ORM: o repositorio devolveu {linha.__name__} em vez de "
        f"{modelo.__name__} (SPEC-0011 secao 3.8)"
    )


# --------------------------------------------------------------------------- #
# 1. Modulos
# --------------------------------------------------------------------------- #
async def test_modulo_faz_round_trip_completo_de_criar_ler_atualizar_e_apagar(uow) -> None:
    gravado = await uow.modules.add(make_module(slug="assistente-alfa", name="Assistente alfa"))

    assert await uow.modules.get(gravado.id) == gravado
    por_slug = await uow.modules.get_by_slug("assistente-alfa")
    assert por_slug is not None and por_slug.id == gravado.id, "chave natural e o slug"
    assert await uow.modules.count() == 1

    atualizado = await uow.modules.update(gravado.model_copy(update={"name": "Assistente beta"}))
    assert atualizado.name == "Assistente beta"
    relido = await uow.modules.get(gravado.id)
    assert relido is not None and relido.name == "Assistente beta"

    await uow.modules.delete(gravado.id)
    assert await uow.modules.get(gravado.id) is None
    assert await uow.modules.count() == 0


async def test_modulos_sao_listados_por_cada_filtro_suportado(uow) -> None:
    await uow.modules.add(
        make_module(
            slug="alfa",
            kind=ModuleKind.AGENT,
            status=ModuleStatus.ACTIVE,
            tags=("producao", "nucleo"),
        )
    )
    await uow.modules.add(
        make_module(
            slug="beta",
            kind=ModuleKind.PIPELINE,
            status=ModuleStatus.DRAFT,
            tags=("nucleo",),
        )
    )

    assert [m.slug for m in await uow.modules.list(kind=ModuleKind.PIPELINE)] == ["beta"]
    assert [m.slug for m in await uow.modules.list(status=ModuleStatus.ACTIVE)] == ["alfa"]
    assert [m.slug for m in await uow.modules.list(search="alf")] == ["alfa"]
    assert [m.slug for m in await uow.modules.list(tags=["producao"])] == ["alfa"]
    assert [m.slug for m in await uow.modules.list(tags=["nucleo"])] == ["alfa", "beta"]
    assert [m.slug for m in await uow.modules.list(limit=1)] == ["alfa"]
    assert [m.slug for m in await uow.modules.list(limit=1, offset=1)] == ["beta"]
    assert await uow.modules.count(kind=ModuleKind.AGENT) == 1
    assert await uow.modules.count() == 2


async def test_slug_de_modulo_duplicado_gera_conflito(uow) -> None:
    await uow.modules.add(make_module(slug="repetido"))

    with pytest.raises(ConflictError):
        await uow.modules.add(make_module(slug="repetido", module_id=id_de("modulo", "outro")))


async def test_atualizar_modulo_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.modules.update(make_module(slug="fantasma", module_id=ID_AUSENTE))


async def test_apagar_modulo_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.modules.delete(ID_AUSENTE)


# --------------------------------------------------------------------------- #
# 2. Prompts
# --------------------------------------------------------------------------- #
async def test_prompt_faz_round_trip_completo_de_criar_ler_atualizar_e_apagar(uow) -> None:
    gravado = await uow.prompts.add(make_prompt(slug="saudacao", template="Ola, {nome}."))

    assert await uow.prompts.get(gravado.id) == gravado
    por_slug = await uow.prompts.get_by_slug("saudacao")
    assert por_slug is not None and por_slug.id == gravado.id
    assert await uow.prompts.count() == 1

    atualizado = await uow.prompts.update(gravado.model_copy(update={"template": "Oi, {nome}."}))
    assert atualizado.template == "Oi, {nome}."

    await uow.prompts.delete(gravado.id)
    assert await uow.prompts.get(gravado.id) is None
    assert await uow.prompts.count() == 0


async def test_prompts_sao_listados_por_cada_filtro_suportado(uow) -> None:
    await uow.prompts.add(make_prompt(slug="ativo", is_active=True))
    await uow.prompts.add(make_prompt(slug="arquivado", is_active=False))

    assert [p.slug for p in await uow.prompts.list(is_active=True)] == ["ativo"]
    assert [p.slug for p in await uow.prompts.list(is_active=False)] == ["arquivado"]
    assert [p.slug for p in await uow.prompts.list(search="arquiv")] == ["arquivado"]
    assert [p.slug for p in await uow.prompts.list(limit=1)] == ["arquivado"]
    assert await uow.prompts.count(is_active=True) == 1
    assert await uow.prompts.count() == 2


async def test_get_by_slug_devolve_a_versao_ativa_de_maior_numero(uow) -> None:
    await uow.prompts.add(make_prompt(slug="triagem", version=1, template="v1"))
    await uow.prompts.add(make_prompt(slug="triagem", version=3, template="v3"))
    await uow.prompts.add(make_prompt(slug="triagem", version=5, template="v5", is_active=False))

    resolvido = await uow.prompts.get_by_slug("triagem")

    assert resolvido is not None
    assert resolvido.version == 3, "a versao 5 esta inativa; a ativa mais alta e a 3"
    assert resolvido.template == "v3"


async def test_list_versions_devolve_o_historico_da_mais_recente_para_a_mais_antiga(uow) -> None:
    for versao in (1, 3, 5):
        await uow.prompts.add(make_prompt(slug="triagem", version=versao))

    assert [p.version for p in await uow.prompts.list_versions("triagem")] == [5, 3, 1]


async def test_par_slug_e_versao_duplicado_gera_conflito(uow) -> None:
    await uow.prompts.add(make_prompt(slug="triagem", version=2))

    with pytest.raises(ConflictError):
        await uow.prompts.add(
            make_prompt(slug="triagem", version=2, prompt_id=id_de("prompt", "outro"))
        )


async def test_atualizar_prompt_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.prompts.update(make_prompt(slug="fantasma", prompt_id=ID_AUSENTE))


async def test_apagar_prompt_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.prompts.delete(ID_AUSENTE)


# --------------------------------------------------------------------------- #
# 3. Politicas de guardrail
# --------------------------------------------------------------------------- #
async def test_politica_de_guardrail_faz_round_trip_completo(uow) -> None:
    gravada = await uow.guardrails.add(make_policy(slug="entrada-alfa"))

    assert await uow.guardrails.get(gravada.id) == gravada
    por_slug = await uow.guardrails.get_by_slug("entrada-alfa")
    assert por_slug is not None and por_slug.id == gravada.id
    assert await uow.guardrails.count() == 1

    atualizada = await uow.guardrails.update(gravada.model_copy(update={"fail_open": True}))
    assert atualizada.fail_open is True

    await uow.guardrails.delete(gravada.id)
    assert await uow.guardrails.get(gravada.id) is None
    assert await uow.guardrails.count() == 0


async def test_politicas_de_guardrail_sao_listadas_por_cada_filtro_suportado(uow) -> None:
    await uow.guardrails.add(make_policy(slug="entrada", stage=GuardrailStage.INPUT))
    await uow.guardrails.add(
        make_policy(slug="saida", stage=GuardrailStage.OUTPUT, is_active=False)
    )

    assert [p.slug for p in await uow.guardrails.list(stage=GuardrailStage.OUTPUT)] == ["saida"]
    assert [p.slug for p in await uow.guardrails.list(is_active=True)] == ["entrada"]
    assert [p.slug for p in await uow.guardrails.list(search="said")] == ["saida"]
    assert [p.slug for p in await uow.guardrails.list(limit=1, offset=1)] == ["saida"]
    assert await uow.guardrails.count(stage=GuardrailStage.INPUT) == 1
    assert await uow.guardrails.count() == 2


async def test_slug_de_politica_duplicado_gera_conflito(uow) -> None:
    await uow.guardrails.add(make_policy(slug="repetida"))

    with pytest.raises(ConflictError):
        await uow.guardrails.add(make_policy(slug="repetida", policy_id=id_de("politica", "outra")))


async def test_atualizar_politica_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.guardrails.update(make_policy(slug="fantasma", policy_id=ID_AUSENTE))


async def test_apagar_politica_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.guardrails.delete(ID_AUSENTE)


# --------------------------------------------------------------------------- #
# 4. Execucoes e passos
# --------------------------------------------------------------------------- #
async def test_execucao_faz_round_trip_completo_de_criar_ler_e_atualizar(uow) -> None:
    gravada = await uow.runs.add(
        make_run(module_slug="assistente", status=RunStatus.RUNNING, created_at=momento(0))
    )

    lida = await uow.runs.get(gravada.id)
    assert lida is not None and lida.id == gravada.id
    assert [r.id for r in await uow.runs.list(module_slug="assistente")] == [gravada.id]
    assert await uow.runs.count() == 1

    atualizada = await uow.runs.update(
        gravada.model_copy(update={"status": RunStatus.SUCCEEDED, "finished_at": momento(5)})
    )
    assert atualizada.status is RunStatus.SUCCEEDED
    relida = await uow.runs.get(gravada.id)
    assert relida is not None and relida.status is RunStatus.SUCCEEDED


async def test_execucoes_sao_listadas_por_cada_filtro_suportado(uow) -> None:
    await uow.runs.add(
        make_run(
            module_slug="alfa",
            status=RunStatus.SUCCEEDED,
            created_at=momento(0),
            run_id=id_de("execucao", "alfa"),
        )
    )
    await uow.runs.add(
        make_run(
            module_slug="beta",
            status=RunStatus.FAILED,
            tenant_id="acme",
            created_at=momento(600),
            run_id=id_de("execucao", "beta"),
        )
    )

    assert [r.module_slug for r in await uow.runs.list(module_slug="alfa")] == ["alfa"]
    assert [r.module_slug for r in await uow.runs.list(status=RunStatus.FAILED)] == ["beta"]
    assert [r.module_slug for r in await uow.runs.list(since=momento(300))] == ["beta"]
    assert [r.module_slug for r in await uow.runs.list(until=momento(300))] == ["alfa"]
    assert [r.module_slug for r in await uow.runs.list(tenant_id="acme")] == ["beta"]
    assert [r.module_slug for r in await uow.runs.list()] == ["beta", "alfa"], (
        "a listagem vai da execucao mais recente para a mais antiga"
    )
    assert await uow.runs.count(status=RunStatus.SUCCEEDED) == 1
    assert await uow.runs.count() == 2


async def test_add_step_atribui_position_sequencial_e_list_steps_ordena(uow) -> None:
    execucao = await uow.runs.add(make_run(status=RunStatus.RUNNING))

    primeiro = await uow.runs.add_step(
        make_step(execucao.id, name="guardrail-entrada", step_id=id_de("passo", "a"))
    )
    segundo = await uow.runs.add_step(
        make_step(execucao.id, name="chamada-llm", step_id=id_de("passo", "b"))
    )
    terceiro = await uow.runs.add_step(
        make_step(execucao.id, name="guardrail-saida", step_id=id_de("passo", "c"))
    )

    assert [primeiro.index, segundo.index, terceiro.index] == [0, 1, 2], (
        "cada passo recebe o proximo indice livre da trilha, mesmo chegando com index=0"
    )
    assert [passo.name for passo in await uow.runs.list_steps(execucao.id)] == [
        "guardrail-entrada",
        "chamada-llm",
        "guardrail-saida",
    ]


async def test_get_de_execucao_devolve_os_passos_ja_carregados(uow) -> None:
    execucao = await uow.runs.add(make_run(status=RunStatus.RUNNING))
    await uow.runs.add_step(make_step(execucao.id, name="chamada-llm"))

    lida = await uow.runs.get(execucao.id)

    assert lida is not None
    assert [passo.name for passo in lida.steps] == ["chamada-llm"]


async def test_adicionar_execucao_com_id_repetido_gera_conflito(uow) -> None:
    execucao = await uow.runs.add(make_run(run_id=id_de("execucao", "unica")))

    with pytest.raises(ConflictError):
        await uow.runs.add(execucao)


async def test_atualizar_execucao_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.runs.update(make_run(run_id=ID_AUSENTE))


# --------------------------------------------------------------------------- #
# 5. Consumo (FinOps)
# --------------------------------------------------------------------------- #
async def test_registro_de_consumo_faz_round_trip_de_criar_listar_e_contar(uow) -> None:
    gravado = await uow.usage.add(
        make_usage_record(module_slug="assistente", model="modelo-a", cost_usd=0.25)
    )

    listados = await uow.usage.list()
    assert [r.id for r in listados] == [gravado.id]
    assert listados[0].cost_usd == pytest.approx(0.25)
    assert await uow.usage.count() == 1


async def test_registros_de_consumo_sao_listados_por_cada_filtro_suportado(uow) -> None:
    await uow.usage.add(
        make_usage_record(
            module_slug="alfa",
            model="modelo-a",
            cost_usd=1.0,
            occurred_at=momento(0),
            record_id=id_de("consumo", "alfa"),
        )
    )
    await uow.usage.add(
        make_usage_record(
            module_slug="beta",
            model="modelo-b",
            cost_usd=2.0,
            tenant_id="acme",
            occurred_at=momento(600),
            record_id=id_de("consumo", "beta"),
        )
    )

    assert [r.module_slug for r in await uow.usage.list(module_slug="alfa")] == ["alfa"]
    assert [r.module_slug for r in await uow.usage.list(model="modelo-b")] == ["beta"]
    assert [r.module_slug for r in await uow.usage.list(since=momento(300))] == ["beta"]
    assert [r.module_slug for r in await uow.usage.list(until=momento(300))] == ["alfa"]
    assert [r.module_slug for r in await uow.usage.list(tenant_id="acme")] == ["beta"]
    assert await uow.usage.count(module_slug="beta") == 1
    assert await uow.usage.count() == 2


async def test_summary_agrega_custo_e_tokens_por_modulo_e_por_modelo(uow) -> None:
    await uow.usage.add(
        make_usage_record(
            module_slug="alfa",
            model="modelo-a",
            usage=TokenUsage.of(1000, 500),
            cost_usd=1.0,
            run_id=id_de("execucao", "r1"),
            occurred_at=momento(0),
            record_id=id_de("consumo", "1"),
        )
    )
    await uow.usage.add(
        make_usage_record(
            module_slug="alfa",
            model="modelo-b",
            usage=TokenUsage.of(1000, 500),
            cost_usd=2.0,
            run_id=id_de("execucao", "r1"),
            occurred_at=momento(1),
            record_id=id_de("consumo", "2"),
        )
    )
    await uow.usage.add(
        make_usage_record(
            module_slug="beta",
            model="modelo-a",
            usage=TokenUsage.of(1000, 500),
            cost_usd=4.0,
            run_id=None,
            occurred_at=momento(2),
            record_id=id_de("consumo", "3"),
        )
    )

    resumo = await uow.usage.summary()

    assert resumo.total_usd == pytest.approx(7.0)
    assert resumo.total_tokens == 4500
    assert resumo.by_module == {"alfa": pytest.approx(3.0), "beta": pytest.approx(4.0)}
    assert resumo.by_model == {"modelo-a": pytest.approx(5.0), "modelo-b": pytest.approx(2.0)}
    assert resumo.runs == 2, "uma execucao distinta mais um registro avulso sem run_id"


async def test_summary_respeita_o_recorte_de_periodo_e_de_modulo(uow) -> None:
    await uow.usage.add(
        make_usage_record(
            module_slug="alfa",
            cost_usd=1.0,
            occurred_at=momento(0),
            record_id=id_de("consumo", "antigo"),
        )
    )
    await uow.usage.add(
        make_usage_record(
            module_slug="beta",
            cost_usd=2.0,
            occurred_at=momento(600),
            record_id=id_de("consumo", "novo"),
        )
    )

    recente = await uow.usage.summary(since=momento(300))

    assert recente.by_module == {"beta": pytest.approx(2.0)}
    assert recente.total_usd == pytest.approx(2.0)


async def test_total_since_soma_o_custo_do_escopo_pedido(uow) -> None:
    await uow.usage.add(
        make_usage_record(
            module_slug="alfa",
            cost_usd=1.0,
            tenant_id="acme",
            occurred_at=momento(0),
            record_id=id_de("consumo", "a"),
        )
    )
    await uow.usage.add(
        make_usage_record(
            module_slug="beta",
            cost_usd=4.0,
            tenant_id="outro",
            occurred_at=momento(60),
            record_id=id_de("consumo", "b"),
        )
    )

    assert await uow.usage.total_since(momento(-10)) == pytest.approx(5.0)
    assert await uow.usage.total_since(momento(-10), scope="module:alfa") == pytest.approx(1.0)
    assert await uow.usage.total_since(momento(-10), scope="tenant:acme") == pytest.approx(1.0)
    assert await uow.usage.total_since(momento(30)) == pytest.approx(4.0)


# --------------------------------------------------------------------------- #
# 6. Orcamentos
# --------------------------------------------------------------------------- #
async def test_orcamento_faz_round_trip_completo_de_criar_ler_atualizar_e_apagar(uow) -> None:
    gravado = await uow.budgets.add(make_budget(name="mensal", scope="global", limit_usd=10.0))

    assert await uow.budgets.get(gravado.id) == gravado
    assert [b.name for b in await uow.budgets.list()] == ["mensal"]

    atualizado = await uow.budgets.update(gravado.model_copy(update={"limit_usd": 25.0}))
    assert atualizado.limit_usd == pytest.approx(25.0)

    await uow.budgets.delete(gravado.id)
    assert await uow.budgets.get(gravado.id) is None
    assert await uow.budgets.list() == []


async def test_orcamentos_sao_listados_por_cada_filtro_suportado(uow) -> None:
    await uow.budgets.add(make_budget(name="global", scope="global"))
    await uow.budgets.add(make_budget(name="do-alfa", scope="module:alfa", is_active=False))

    assert [b.name for b in await uow.budgets.list(scope="module:alfa")] == ["do-alfa"]
    assert [b.name for b in await uow.budgets.list(is_active=True)] == ["global"]
    assert [b.name for b in await uow.budgets.list(is_active=False)] == ["do-alfa"]


async def test_atualizar_orcamento_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.budgets.update(make_budget(name="fantasma", budget_id=ID_AUSENTE))


async def test_apagar_orcamento_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.budgets.delete(ID_AUSENTE)


# --------------------------------------------------------------------------- #
# 7. Documentos e chunks
# --------------------------------------------------------------------------- #
async def test_documento_faz_round_trip_completo_de_criar_ler_atualizar_e_apagar(uow) -> None:
    gravado = await uow.documents.add(make_document(title="Manual", collection="suporte"))

    assert await uow.documents.get(gravado.id) == gravado
    assert [d.title for d in await uow.documents.list(collection="suporte")] == ["Manual"]
    assert await uow.documents.count() == 1

    atualizado = await uow.documents.update(gravado.model_copy(update={"title": "Manual v2"}))
    assert atualizado.title == "Manual v2"

    await uow.documents.delete(gravado.id)
    assert await uow.documents.get(gravado.id) is None
    assert await uow.documents.count() == 0


async def test_documentos_sao_listados_por_cada_filtro_suportado(uow) -> None:
    await uow.documents.add(
        make_document(title="Manual", collection="suporte", document_id=id_de("doc", "1"))
    )
    await uow.documents.add(
        make_document(title="Politica", collection="juridico", document_id=id_de("doc", "2"))
    )

    assert [d.title for d in await uow.documents.list(collection="juridico")] == ["Politica"]
    assert [d.title for d in await uow.documents.list(search="Manu")] == ["Manual"]
    assert len(await uow.documents.list(limit=1)) == 1
    assert await uow.documents.count(collection="suporte") == 1
    assert await uow.documents.count() == 2
    assert await uow.documents.collections() == ["juridico", "suporte"]


async def test_chunks_sao_gravados_listados_em_ordem_e_apagados_pelo_documento(uow) -> None:
    documento = await uow.documents.add(make_document())

    gravados = await uow.documents.add_chunks(
        [
            make_chunk(documento.id, index=posicao, content=f"trecho {posicao}")
            for posicao in (2, 0, 1)
        ]
    )

    assert gravados == 3
    assert [c.index for c in await uow.documents.list_chunks(documento.id)] == [0, 1, 2]
    assert await uow.documents.delete_chunks(documento.id) == 3
    assert await uow.documents.list_chunks(documento.id) == []


async def test_apagar_documento_apaga_os_chunks_em_cascata(uow) -> None:
    documento = await uow.documents.add(make_document())
    await uow.documents.add_chunks(
        [make_chunk(documento.id, index=posicao) for posicao in range(3)]
    )
    assert len(await uow.documents.list_chunks(documento.id)) == 3

    await uow.documents.delete(documento.id)

    assert await uow.documents.list_chunks(documento.id) == [], (
        "ON DELETE CASCADE de chunks.document_id exige PRAGMA foreign_keys=ON (SPEC-0011 secao 9)"
    )


async def test_atualizar_documento_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.documents.update(make_document(document_id=ID_AUSENTE))


async def test_apagar_documento_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.documents.delete(ID_AUSENTE)


# --------------------------------------------------------------------------- #
# 8. Usuarios
# --------------------------------------------------------------------------- #
async def test_usuario_faz_round_trip_completo_de_criar_ler_atualizar_e_apagar(uow) -> None:
    gravado = await uow.users.add(make_user(email="operador@lukato.local"))

    assert await uow.users.get(gravado.id) == gravado
    por_email = await uow.users.get_by_email("operador@lukato.local")
    assert por_email is not None and por_email.id == gravado.id, "chave natural e o e-mail"
    assert [u.id for u in await uow.users.list()] == [gravado.id]
    assert await uow.users.count() == 1

    atualizado = await uow.users.update(gravado.model_copy(update={"is_active": False}))
    assert atualizado.is_active is False

    await uow.users.delete(gravado.id)
    assert await uow.users.get(gravado.id) is None
    assert await uow.users.count() == 0


async def test_usuarios_sao_contados_por_cada_filtro_suportado(uow) -> None:
    await uow.users.add(make_user(email="admin@lukato.local", role=Role.ADMIN))
    await uow.users.add(
        make_user(email="leitor@lukato.local", role=Role.VIEWER, is_active=False, tenant_id="acme")
    )

    assert await uow.users.count(role=Role.ADMIN) == 1
    assert await uow.users.count(is_active=True) == 1
    assert await uow.users.count(tenant_id="acme") == 1
    assert await uow.users.count(search="leitor") == 1
    assert await uow.users.count() == 2
    assert len(await uow.users.list(limit=1)) == 1


async def test_email_de_usuario_duplicado_gera_conflito(uow) -> None:
    await uow.users.add(make_user(email="repetido@lukato.local"))

    with pytest.raises(ConflictError):
        await uow.users.add(
            make_user(email="repetido@lukato.local", user_id=id_de("usuario", "outro"))
        )


async def test_atualizar_usuario_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.users.update(make_user(email="fantasma@lukato.local", user_id=ID_AUSENTE))


async def test_apagar_usuario_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.users.delete(ID_AUSENTE)


# --------------------------------------------------------------------------- #
# 9. Chaves de API
# --------------------------------------------------------------------------- #
async def test_chave_de_api_faz_round_trip_completo_de_criar_ler_atualizar_e_apagar(uow) -> None:
    gravada = await uow.api_keys.add(make_api_key(prefix="lkt_alfa"))

    assert await uow.api_keys.get(gravada.id) == gravada
    por_prefixo = await uow.api_keys.get_by_prefix("lkt_alfa")
    assert por_prefixo is not None and por_prefixo.id == gravada.id, "chave natural e o prefixo"
    assert [k.prefix for k in await uow.api_keys.list()] == ["lkt_alfa"]

    atualizada = await uow.api_keys.update(gravada.model_copy(update={"is_active": False}))
    assert atualizada.is_active is False

    await uow.api_keys.delete(gravada.id)
    assert await uow.api_keys.get(gravada.id) is None
    assert await uow.api_keys.list() == []


async def test_chaves_de_api_sao_listadas_pelo_filtro_de_atividade(uow) -> None:
    await uow.api_keys.add(make_api_key(prefix="lkt_ativa"))
    await uow.api_keys.add(
        make_api_key(prefix="lkt_inativa", is_active=False, api_key_id=id_de("api-key", "2"))
    )

    assert [k.prefix for k in await uow.api_keys.list(is_active=True)] == ["lkt_ativa"]
    assert [k.prefix for k in await uow.api_keys.list(is_active=False)] == ["lkt_inativa"]
    assert len(await uow.api_keys.list(limit=1)) == 1


async def test_touch_registra_o_instante_do_ultimo_uso_da_chave(uow) -> None:
    gravada = await uow.api_keys.add(make_api_key(prefix="lkt_uso"))
    assert gravada.last_used_at is None

    await uow.api_keys.touch(gravada.id, AGORA)

    relida = await uow.api_keys.get_by_prefix("lkt_uso")
    assert relida is not None and relida.last_used_at == AGORA


async def test_prefixo_de_chave_duplicado_gera_conflito(uow) -> None:
    await uow.api_keys.add(make_api_key(prefix="lkt_repetido"))

    with pytest.raises(ConflictError):
        await uow.api_keys.add(
            make_api_key(prefix="lkt_repetido", api_key_id=id_de("api-key", "outra"))
        )


async def test_atualizar_chave_de_api_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.api_keys.update(make_api_key(prefix="lkt_fantasma", api_key_id=ID_AUSENTE))


async def test_apagar_chave_de_api_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.api_keys.delete(ID_AUSENTE)


# --------------------------------------------------------------------------- #
# 10. Comerciais e assinaturas
# --------------------------------------------------------------------------- #
async def test_comercial_faz_round_trip_completo_de_criar_ler_atualizar_e_apagar(uow) -> None:
    gravado = await uow.commercials.add(make_commercial(code="COM_000001"))

    assert await uow.commercials.get(gravado.id) == gravado
    por_codigo = await uow.commercials.get_by_code("COM_000001")
    assert por_codigo is not None and por_codigo.id == gravado.id, "chave natural e o codigo"
    assert await uow.commercials.count() == 1

    atualizado = await uow.commercials.update(gravado.model_copy(update={"brand": "Vivo"}))
    assert atualizado.brand == "Vivo"

    await uow.commercials.delete(gravado.id)
    assert await uow.commercials.get(gravado.id) is None
    assert await uow.commercials.count() == 0


async def test_comerciais_sao_listados_por_cada_filtro_suportado(uow) -> None:
    await uow.commercials.add(
        make_commercial(
            code="COM_000001",
            brand="Claro",
            campaign="Verao",
            text="o melhor plano da claro",
        )
    )
    await uow.commercials.add(
        make_commercial(
            code="COM_000002",
            brand="Vivo",
            campaign="Inverno",
            text="fibra da vivo em casa",
            is_active=False,
        )
    )

    assert [c.commercial_id for c in await uow.commercials.list(brand="Vivo")] == ["COM_000002"]
    assert [c.commercial_id for c in await uow.commercials.list(campaign="Verao")] == ["COM_000001"]
    assert [c.commercial_id for c in await uow.commercials.list(search="fibra")] == ["COM_000002"]
    assert [c.commercial_id for c in await uow.commercials.list(is_active=True)] == ["COM_000001"]
    assert len(await uow.commercials.list(limit=1)) == 1
    assert await uow.commercials.count(brand="Claro") == 1
    assert await uow.commercials.count() == 2
    assert [c.commercial_id for c in await uow.commercials.all_active()] == ["COM_000001"]


async def test_codigo_de_comercial_duplicado_gera_conflito(uow) -> None:
    await uow.commercials.add(make_commercial(code="COM_REPETIDO"))

    with pytest.raises(ConflictError):
        await uow.commercials.add(
            make_commercial(code="COM_REPETIDO", commercial_id=id_de("comercial", "outro"))
        )


async def test_upsert_de_assinatura_substitui_a_anterior_sem_duplicar(uow) -> None:
    comercial = await uow.commercials.add(make_commercial())

    primeira = await uow.commercials.upsert_fingerprint(
        make_fingerprint(comercial.id, normalized_text="texto original")
    )
    segunda = await uow.commercials.upsert_fingerprint(
        make_fingerprint(comercial.id, normalized_text="texto revisado")
    )

    assert primeira.id == segunda.id, "a assinatura e unica por comercial"
    assert segunda.normalized_text == "texto revisado"
    assert len(await uow.commercials.list_fingerprints()) == 1
    guardada = await uow.commercials.get_fingerprint(comercial.id)
    assert guardada is not None and guardada.normalized_text == "texto revisado"


async def test_apagar_comercial_apaga_a_assinatura_em_cascata(uow) -> None:
    comercial = await uow.commercials.add(make_commercial())
    await uow.commercials.upsert_fingerprint(make_fingerprint(comercial.id))
    assert await uow.commercials.get_fingerprint(comercial.id) is not None

    await uow.commercials.delete(comercial.id)

    assert await uow.commercials.get_fingerprint(comercial.id) is None
    assert await uow.commercials.list_fingerprints() == []


async def test_atualizar_comercial_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.commercials.update(make_commercial(code="COM_FANTASMA", commercial_id=ID_AUSENTE))


async def test_apagar_comercial_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.commercials.delete(ID_AUSENTE)


# --------------------------------------------------------------------------- #
# 11. Midia e artefatos derivados
# --------------------------------------------------------------------------- #
async def test_midia_faz_round_trip_completo_de_criar_ler_atualizar_e_apagar(uow) -> None:
    gravada = await uow.media.add(make_media(uri="file:///midia/alfa.mp4", title="Alfa"))

    assert await uow.media.get(gravada.id) == gravada
    assert [m.title for m in await uow.media.list(search="alfa.mp4")] == ["Alfa"], (
        "a busca de midia cobre titulo e URI — a URI e a chave natural do ativo"
    )
    assert await uow.media.count() == 1

    atualizada = await uow.media.update(gravada.model_copy(update={"status": "processed"}))
    assert atualizada.status == "processed"

    await uow.media.delete(gravada.id)
    assert await uow.media.get(gravada.id) is None
    assert await uow.media.count() == 0


async def test_midias_sao_listadas_por_cada_filtro_suportado(uow) -> None:
    await uow.media.add(make_media(uri="file:///midia/alfa.mp4", title="Alfa", status="registered"))
    await uow.media.add(make_media(uri="file:///midia/beta.mp4", title="Beta", status="processed"))

    assert [m.title for m in await uow.media.list(status="processed")] == ["Beta"]
    assert [m.title for m in await uow.media.list(search="Alf")] == ["Alfa"]
    assert len(await uow.media.list(limit=1)) == 1
    assert await uow.media.count(status="registered") == 1
    assert await uow.media.count() == 2


async def test_transcricao_e_substituida_e_nao_duplicada_ao_gravar_de_novo(uow) -> None:
    midia = await uow.media.add(make_media())

    primeira = await uow.media.save_transcript(
        make_transcript([("ola mundo", 0.0, 2.0)], media_id=midia.id)
    )
    segunda = await uow.media.save_transcript(
        make_transcript([("ola mundo cruel", 0.0, 3.0)], media_id=midia.id)
    )

    assert primeira.id == segunda.id, "a transcricao e unica por ativo de midia"
    guardada = await uow.media.get_transcript(midia.id)
    assert guardada is not None
    assert [palavra.word for palavra in guardada.words] == ["ola", "mundo", "cruel"]


async def test_save_scenes_duas_vezes_nao_duplica_os_cortes_de_cena(uow) -> None:
    midia = await uow.media.add(make_media())
    cenas = make_scenes([(0.0, 5.0), (5.0, 12.0)])

    primeira = await uow.media.save_scenes(midia.id, cenas)
    segunda = await uow.media.save_scenes(midia.id, cenas)

    assert primeira == segunda == 2
    guardadas = await uow.media.list_scenes(midia.id)
    assert len(guardadas) == 2, "save_scenes substitui a lista inteira, nunca acumula"
    assert [cena.index for cena in guardadas] == [0, 1]


async def test_save_ocr_duas_vezes_nao_duplica_os_textos_reconhecidos(uow) -> None:
    midia = await uow.media.add(make_media())
    textos = make_ocr([("assine ja", 1.0, 2.0), ("claro", 3.0, 4.0)])

    primeira = await uow.media.save_ocr(midia.id, textos)
    segunda = await uow.media.save_ocr(midia.id, textos)

    assert primeira == segunda == 2
    guardados = await uow.media.list_ocr(midia.id)
    assert len(guardados) == 2, "save_ocr substitui a lista inteira, nunca acumula"
    assert [texto.start for texto in guardados] == [1.0, 3.0]


async def test_apagar_midia_apaga_transcricao_cenas_ocr_e_deteccoes_em_cascata(uow) -> None:
    midia = await uow.media.add(make_media())
    comercial = await uow.commercials.add(make_commercial())
    await uow.media.save_transcript(make_transcript([("ola", 0.0, 1.0)], media_id=midia.id))
    await uow.media.save_scenes(midia.id, make_scenes([(0.0, 5.0)]))
    await uow.media.save_ocr(midia.id, make_ocr([("claro", 1.0, 2.0)]))
    await uow.detections.add(make_detection(media_id=midia.id, commercial_id=comercial.id))

    await uow.media.delete(midia.id)

    assert await uow.media.get_transcript(midia.id) is None
    assert await uow.media.list_scenes(midia.id) == []
    assert await uow.media.list_ocr(midia.id) == []
    assert await uow.detections.count(media_id=midia.id) == 0, (
        "as quatro cascatas de media_assets exigem PRAGMA foreign_keys=ON (SPEC-0011 secao 9)"
    )


async def test_atualizar_midia_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.media.update(make_media(media_id=ID_AUSENTE))


async def test_apagar_midia_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.media.delete(ID_AUSENTE)


# --------------------------------------------------------------------------- #
# 12. Deteccoes
# --------------------------------------------------------------------------- #
async def test_deteccao_faz_round_trip_completo_de_criar_ler_atualizar_e_apagar(uow) -> None:
    midia = await uow.media.add(make_media())
    comercial = await uow.commercials.add(make_commercial())

    gravada = await uow.detections.add(
        make_detection(media_id=midia.id, commercial_id=comercial.id)
    )

    assert await uow.detections.get(gravada.id) == gravada
    assert [d.id for d in await uow.detections.list(media_id=midia.id)] == [gravada.id], (
        "a chave natural de consulta e o par (midia, janela temporal)"
    )
    assert await uow.detections.count() == 1

    atualizada = await uow.detections.update(
        gravada.model_copy(update={"status": DetectionStatus.REJECTED, "verified_by_vlm": True})
    )
    assert atualizada.status is DetectionStatus.REJECTED
    assert atualizada.verified_by_vlm is True

    await uow.detections.delete(gravada.id)
    assert await uow.detections.get(gravada.id) is None
    assert await uow.detections.count() == 0


async def test_deteccoes_sao_listadas_por_cada_filtro_suportado(uow) -> None:
    midia_a = await uow.media.add(make_media(uri="file:///midia/a.mp4"))
    midia_b = await uow.media.add(make_media(uri="file:///midia/b.mp4"))
    comercial_a = await uow.commercials.add(make_commercial(code="COM_A"))
    comercial_b = await uow.commercials.add(make_commercial(code="COM_B"))
    await uow.detections.add_many(
        [
            make_detection(
                media_id=midia_a.id,
                commercial_id=comercial_a.id,
                start=10.0,
                end=40.0,
                status=DetectionStatus.ACCEPTED,
                detection_id=id_de("deteccao", "1"),
            ),
            make_detection(
                media_id=midia_a.id,
                commercial_id=comercial_b.id,
                start=50.0,
                end=70.0,
                status=DetectionStatus.NEEDS_REVIEW,
                detection_id=id_de("deteccao", "2"),
            ),
            make_detection(
                media_id=midia_b.id,
                commercial_id=comercial_a.id,
                start=5.0,
                end=9.0,
                status=DetectionStatus.REJECTED,
                detection_id=id_de("deteccao", "3"),
            ),
        ]
    )

    assert len(await uow.detections.list(media_id=midia_a.id)) == 2
    assert len(await uow.detections.list(commercial_id=comercial_a.id)) == 2
    assert len(await uow.detections.list(status=DetectionStatus.NEEDS_REVIEW)) == 1
    assert len(await uow.detections.list(limit=2)) == 2
    assert [d.start for d in await uow.detections.list()] == [5.0, 10.0, 50.0], (
        "a listagem segue a linha do tempo"
    )
    assert await uow.detections.count(media_id=midia_a.id) == 2
    assert await uow.detections.count() == 3


async def test_add_many_preserva_a_ordem_recebida_das_deteccoes(uow) -> None:
    midia = await uow.media.add(make_media())
    comercial = await uow.commercials.add(make_commercial())
    pedidas = [
        make_detection(
            media_id=midia.id,
            commercial_id=comercial.id,
            start=inicio,
            end=inicio + 5.0,
            detection_id=id_de("deteccao", inicio),
        )
        for inicio in (30.0, 10.0, 20.0)
    ]

    gravadas = await uow.detections.add_many(pedidas)

    assert [d.start for d in gravadas] == [30.0, 10.0, 20.0]


async def test_delete_by_media_remove_todas_as_deteccoes_do_ativo(uow) -> None:
    midia_a = await uow.media.add(make_media(uri="file:///midia/a.mp4"))
    midia_b = await uow.media.add(make_media(uri="file:///midia/b.mp4"))
    comercial = await uow.commercials.add(make_commercial())
    await uow.detections.add_many(
        [
            make_detection(
                media_id=midia_a.id, commercial_id=comercial.id, detection_id=id_de("det", "a1")
            ),
            make_detection(
                media_id=midia_a.id,
                commercial_id=comercial.id,
                start=60.0,
                end=90.0,
                detection_id=id_de("det", "a2"),
            ),
            make_detection(
                media_id=midia_b.id, commercial_id=comercial.id, detection_id=id_de("det", "b1")
            ),
        ]
    )

    removidas = await uow.detections.delete_by_media(midia_a.id)

    assert removidas == 2
    assert await uow.detections.count(media_id=midia_a.id) == 0
    assert await uow.detections.count(media_id=midia_b.id) == 1


async def test_atualizar_deteccao_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.detections.update(make_detection(detection_id=ID_AUSENTE))


async def test_apagar_deteccao_inexistente_gera_not_found(uow) -> None:
    with pytest.raises(NotFoundError):
        await uow.detections.delete(ID_AUSENTE)


# --------------------------------------------------------------------------- #
# 13. Fronteira do adaptador: nenhum objeto ORM escapa
# --------------------------------------------------------------------------- #
async def test_nenhum_dos_doze_repositorios_devolve_objeto_orm(uow) -> None:
    """Cada repositorio devolve o modelo de dominio, nunca a linha (SPEC-0011 §3.8)."""
    modulo = await uow.modules.add(make_module(slug="fronteira"))
    prova_que_e_dominio(modulo, ModuleDefinition, orm.ModuleRow)
    prova_que_e_dominio(await uow.modules.get(modulo.id), ModuleDefinition, orm.ModuleRow)
    prova_que_e_dominio((await uow.modules.list())[0], ModuleDefinition, orm.ModuleRow)

    prompt = await uow.prompts.add(make_prompt(slug="fronteira"))
    prova_que_e_dominio(prompt, PromptTemplate, orm.PromptRow)
    prova_que_e_dominio(await uow.prompts.get_by_slug("fronteira"), PromptTemplate, orm.PromptRow)

    politica = await uow.guardrails.add(make_policy(slug="fronteira"))
    prova_que_e_dominio(politica, GuardrailPolicy, orm.GuardrailPolicyRow)

    execucao = await uow.runs.add(make_run(status=RunStatus.RUNNING))
    prova_que_e_dominio(execucao, AgentRun, orm.AgentRunRow)
    passo = await uow.runs.add_step(make_step(execucao.id))
    prova_que_e_dominio(passo, RunStep, orm.RunStepRow)
    prova_que_e_dominio((await uow.runs.list_steps(execucao.id))[0], RunStep, orm.RunStepRow)

    consumo = await uow.usage.add(make_usage_record())
    prova_que_e_dominio(consumo, UsageRecord, orm.UsageRecordRow)
    prova_que_e_dominio((await uow.usage.list())[0], UsageRecord, orm.UsageRecordRow)

    orcamento = await uow.budgets.add(make_budget())
    prova_que_e_dominio(orcamento, Budget, orm.BudgetRow)

    documento = await uow.documents.add(make_document())
    prova_que_e_dominio(documento, Document, orm.DocumentRow)
    await uow.documents.add_chunks([make_chunk(documento.id)])
    prova_que_e_dominio((await uow.documents.list_chunks(documento.id))[0], Chunk, orm.ChunkRow)

    usuario = await uow.users.add(make_user())
    prova_que_e_dominio(usuario, User, orm.UserRow)
    prova_que_e_dominio(await uow.users.get_by_email(usuario.email), User, orm.UserRow)

    chave = await uow.api_keys.add(make_api_key())
    prova_que_e_dominio(chave, ApiKey, orm.ApiKeyRow)
    prova_que_e_dominio(await uow.api_keys.get_by_prefix(chave.prefix), ApiKey, orm.ApiKeyRow)

    comercial = await uow.commercials.add(make_commercial())
    prova_que_e_dominio(comercial, Commercial, orm.CommercialRow)
    assinatura = await uow.commercials.upsert_fingerprint(make_fingerprint(comercial.id))
    prova_que_e_dominio(assinatura, AdFingerprint, orm.AdFingerprintRow)

    midia = await uow.media.add(make_media())
    prova_que_e_dominio(midia, MediaAsset, orm.MediaAssetRow)
    transcricao = await uow.media.save_transcript(
        make_transcript([("ola", 0.0, 1.0)], media_id=midia.id)
    )
    prova_que_e_dominio(transcricao, Transcript, orm.TranscriptRow)
    await uow.media.save_scenes(midia.id, make_scenes([(0.0, 5.0)]))
    prova_que_e_dominio((await uow.media.list_scenes(midia.id))[0], SceneCut, orm.SceneCutRow)
    await uow.media.save_ocr(midia.id, make_ocr([("claro", 1.0, 2.0)]))
    prova_que_e_dominio((await uow.media.list_ocr(midia.id))[0], OcrText, orm.OcrTextRow)

    deteccao = await uow.detections.add(
        make_detection(media_id=midia.id, commercial_id=comercial.id)
    )
    prova_que_e_dominio(deteccao, Detection, orm.DetectionRow)
    prova_que_e_dominio((await uow.detections.list())[0], Detection, orm.DetectionRow)


# --------------------------------------------------------------------------- #
# 14. Unidade de trabalho
# --------------------------------------------------------------------------- #
async def test_commit_persiste_as_escritas_para_a_transacao_seguinte(
    uow_factory: UnitOfWorkFactoryImpl,
) -> None:
    async with uow_factory() as unidade:
        await unidade.modules.add(make_module(slug="persistido"))
        await unidade.commit()

    async with uow_factory() as outra:
        assert await outra.modules.get_by_slug("persistido") is not None


async def test_rollback_descarta_as_escritas_pendentes(
    uow_factory: UnitOfWorkFactoryImpl,
) -> None:
    async with uow_factory() as unidade:
        await unidade.modules.add(make_module(slug="descartado"))
        await unidade.rollback()

    async with uow_factory() as outra:
        assert await outra.modules.get_by_slug("descartado") is None


async def test_excecao_dentro_do_async_with_faz_rollback_automatico(
    uow_factory: UnitOfWorkFactoryImpl,
) -> None:
    class FalhaDoCasoDeUso(RuntimeError):
        """Erro de negocio levantado no meio da transacao."""

    with pytest.raises(FalhaDoCasoDeUso):
        async with uow_factory() as unidade:
            await unidade.modules.add(make_module(slug="abortado"))
            raise FalhaDoCasoDeUso("o caso de uso falhou depois de gravar")

    async with uow_factory() as outra:
        assert await outra.modules.get_by_slug("abortado") is None, (
            "sair do contexto por excecao tem de desfazer tudo o que foi gravado"
        )


async def test_integrity_error_no_commit_vira_conflict_error(
    uow_factory: UnitOfWorkFactoryImpl,
) -> None:
    async with uow_factory() as unidade:
        for sufixo in ("a", "b"):
            unidade.session.add(
                orm.UserRow(
                    id=id_de("usuario", sufixo),
                    email="colisao@lukato.local",
                    name=f"Usuario {sufixo}",
                    role=Role.VIEWER.value,
                    password_hash="",
                    is_active=True,
                    tenant_id="default",
                    created_at=AGORA,
                    updated_at=AGORA,
                )
            )

        with pytest.raises(ConflictError):
            await unidade.commit()


async def test_repositorio_acessado_fora_do_contexto_gera_erro_de_configuracao(
    uow_factory: UnitOfWorkFactoryImpl,
) -> None:
    unidade = uow_factory()

    with pytest.raises(ConfigurationError):
        _ = unidade.modules


async def test_unidade_de_trabalho_expoe_os_doze_repositorios_da_porta(uow) -> None:
    from lukato.adapters.persistence.uow import REPOSITORY_ATTRS

    assert len(REPOSITORY_ATTRS) == 12
    ausentes = [nome for nome in REPOSITORY_ATTRS if getattr(uow, nome, None) is None]
    assert ausentes == [], f"repositorios nao instanciados pela unidade de trabalho: {ausentes}"
