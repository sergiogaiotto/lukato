"""Testes de unidade do registry de building blocks (SPEC-0002 secoes 2, 3 e 4).

O nucleo nao conhece nenhum modulo em tempo de compilacao: eles chegam pelo
decorator, pelos embutidos carregados por nome e pelos entry points do grupo
`lukato.modules`. O que esta suite prova, na ordem da SPEC-0002:

* `register` valida subclasse, slug, `kind` e `handle` implementado; slug repetido
  por **outra** classe da `ConflictError`, salvo `replace=True`;
* `instantiate` mantem **uma** instancia por slug;
* `discover` sobrevive a um entry point quebrado: WARNING, `discover_errors` e segue;
* `load_builtin` registra os cinco embutidos;
* `clear()` devolve o isolamento que os testes precisam.

A fixture `registry` do `conftest.py` entrega o singleton ja esvaziado antes e
depois de cada teste, entao nada vaza de um caso para o outro.
"""

from __future__ import annotations

import importlib
from typing import Any, ClassVar

import pytest

from lukato.domain.errors import ConflictError, ModuleError, ModuleNotFound, ValidationError
from lukato.domain.models.module import ModuleKind
from lukato.modules.base import (
    BaseModule,
    ModuleContext,
    ModuleRequest,
    ModuleResponse,
    UIDescriptor,
    UINavItem,
)
from lukato.modules.registry import (
    BUILTIN_MODULE_NAMES,
    DEFAULT_ENTRY_POINT_GROUP,
    ModuleRegistry,
    register_module,
)
from lukato.modules.registry import registry as registry_singleton

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Modulos de teste
# --------------------------------------------------------------------------- #
class ModuloDeTeste(BaseModule):
    """Building block minimo e valido, usado como cobaia do registry."""

    kind = ModuleKind.AGENT
    slug = "modulo-de-teste"
    name = "Modulo de teste"
    description = "Building block criado apenas para exercitar o registry."
    version = "2.1.0"
    capabilities = ("eco",)
    config_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"prefixo": {"type": "string"}},
    }

    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Ecoa a entrada recebida."""
        return ModuleResponse.text(request.input)

    def ui(self) -> UIDescriptor:
        """Publica um item de menu para provar que `describe()` le a UI."""
        return UIDescriptor(
            nav=[UINavItem(label="Teste", icon="beaker", endpoint="/teste")], accent="#000000"
        )


class OutroModuloDeTeste(BaseModule):
    """Segundo building block com o **mesmo** slug, para exercitar o conflito."""

    kind = ModuleKind.TOOL
    slug = "modulo-de-teste"
    name = "Outro modulo de teste"

    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Devolve uma resposta fixa."""
        return ModuleResponse.text("outro")


class ModuloComSlugInvalido(BaseModule):
    """Slug em maiusculas com underscore: fora do padrao `^[a-z0-9][a-z0-9-]{1,62}$`."""

    kind = ModuleKind.CUSTOM
    slug = "Modulo_Invalido"
    name = "Slug invalido"

    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Nunca chega a ser chamado: o registro e recusado antes."""
        return ModuleResponse.text("")


class ModuloSemHandle(BaseModule):
    """Subclasse que nao implementa `handle`: continua abstrata."""

    kind = ModuleKind.CUSTOM
    slug = "modulo-sem-handle"
    name = "Sem handle"


class ModuloQueFalhaAoNascer(BaseModule):
    """Building block cujo construtor estoura, para exercitar `instantiate`."""

    kind = ModuleKind.CUSTOM
    slug = "modulo-que-falha"
    name = "Falha ao nascer"

    def __init__(self) -> None:
        raise RuntimeError("dependencia ausente no construtor")

    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Nunca chega a ser chamado."""
        return ModuleResponse.text("")


class _EntryPointFalso:
    """Duble de `importlib.metadata.EntryPoint` com carga programavel."""

    def __init__(self, nome: str, resultado: Any = None, erro: Exception | None = None) -> None:
        self.name = nome
        self.group = DEFAULT_ENTRY_POINT_GROUP
        self.value = f"tests.unit.test_registry:{nome}"
        self._resultado = resultado
        self._erro = erro

    def load(self) -> Any:
        """Devolve a classe programada ou levanta o erro programado."""
        if self._erro is not None:
            raise self._erro
        return self._resultado


# --------------------------------------------------------------------------- #
# Registro
# --------------------------------------------------------------------------- #
def test_register_devolve_a_propria_classe_e_a_torna_consultavel(
    registry: ModuleRegistry,
) -> None:
    devolvida = registry.register(ModuloDeTeste)

    assert devolvida is ModuloDeTeste
    assert registry.get("modulo-de-teste") is ModuloDeTeste
    assert "modulo-de-teste" in registry
    assert len(registry) == 1


def test_registrar_slug_duplicado_por_outra_classe_levanta_conflict_error(
    registry: ModuleRegistry,
) -> None:
    registry.register(ModuloDeTeste)

    with pytest.raises(ConflictError) as capturado:
        registry.register(OutroModuloDeTeste)

    assert capturado.value.http_status == 409
    assert capturado.value.details["slug"] == "modulo-de-teste"
    assert registry.get("modulo-de-teste") is ModuloDeTeste, "o registro original permanece"


def test_registrar_a_mesma_classe_duas_vezes_e_idempotente(registry: ModuleRegistry) -> None:
    registry.register(ModuloDeTeste)
    registry.register(ModuloDeTeste)

    assert len(registry) == 1, (
        "os embutidos sao alcancaveis por dois caminhos; repetir a mesma classe nao e conflito"
    )


def test_replace_true_substitui_a_classe_e_descarta_a_instancia_antiga(
    registry: ModuleRegistry,
) -> None:
    registry.register(ModuloDeTeste)
    antiga = registry.instantiate("modulo-de-teste")

    registry.register(OutroModuloDeTeste, replace=True)

    assert registry.get("modulo-de-teste") is OutroModuloDeTeste
    nova = registry.instantiate("modulo-de-teste")
    assert nova is not antiga
    assert isinstance(nova, OutroModuloDeTeste)


def test_registrar_slug_fora_do_padrao_levanta_validation_error(
    registry: ModuleRegistry,
) -> None:
    with pytest.raises(ValidationError) as capturado:
        registry.register(ModuloComSlugInvalido)

    assert capturado.value.details["slug"] == "Modulo_Invalido"
    assert "pattern" in capturado.value.details
    assert len(registry) == 0


def test_registrar_classe_sem_handle_levanta_validation_error(
    registry: ModuleRegistry,
) -> None:
    with pytest.raises(ValidationError) as capturado:
        registry.register(ModuloSemHandle)

    assert "handle" in str(capturado.value)


def test_registrar_algo_que_nao_e_building_block_levanta_validation_error(
    registry: ModuleRegistry,
) -> None:
    with pytest.raises(ValidationError):
        registry.register(dict)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        registry.register(BaseModule)


def test_decorator_register_module_usa_o_singleton_do_processo(
    registry: ModuleRegistry,
) -> None:
    devolvida = register_module(ModuloDeTeste)

    assert devolvida is ModuloDeTeste
    assert registry_singleton.get("modulo-de-teste") is ModuloDeTeste


def test_unregister_remove_o_modulo_e_slug_desconhecido_levanta_module_not_found(
    registry: ModuleRegistry,
) -> None:
    registry.register(ModuloDeTeste)

    registry.unregister("modulo-de-teste")

    assert "modulo-de-teste" not in registry
    with pytest.raises(ModuleNotFound):
        registry.unregister("modulo-de-teste")


def test_get_de_slug_desconhecido_levanta_module_not_found(registry: ModuleRegistry) -> None:
    with pytest.raises(ModuleNotFound) as capturado:
        registry.get("nao-existe")

    assert capturado.value.http_status == 404
    assert capturado.value.details["slug"] == "nao-existe"


# --------------------------------------------------------------------------- #
# Instanciacao
# --------------------------------------------------------------------------- #
def test_instantiate_devolve_sempre_a_mesma_instancia_por_slug(
    registry: ModuleRegistry,
) -> None:
    registry.register(ModuloDeTeste)

    primeira = registry.instantiate("modulo-de-teste")
    segunda = registry.instantiate("modulo-de-teste")

    assert primeira is segunda, "o registry mantem uma instancia por slug (SPEC-0002 regra 3)"


def test_instantiate_de_modulo_que_falha_ao_nascer_vira_module_error(
    registry: ModuleRegistry,
) -> None:
    registry.register(ModuloQueFalhaAoNascer)

    with pytest.raises(ModuleError) as capturado:
        registry.instantiate("modulo-que-falha")

    assert capturado.value.details["slug"] == "modulo-que-falha"


# --------------------------------------------------------------------------- #
# Descricao
# --------------------------------------------------------------------------- #
def test_describe_reproduz_os_metadados_declarados_pela_classe(
    registry: ModuleRegistry,
) -> None:
    registry.register(ModuloDeTeste)

    (descritor,) = registry.describe()

    assert descritor.slug == "modulo-de-teste"
    assert descritor.name == "Modulo de teste"
    assert descritor.kind is ModuleKind.AGENT
    assert descritor.version == "2.1.0"
    assert descritor.capabilities == ["eco"]
    assert descritor.config_schema["type"] == "object"
    assert descritor.source == "builtin"
    assert descritor.class_path.endswith(":ModuloDeTeste")
    assert descritor.ui.nav[0].endpoint == "/teste"


def test_describe_e_all_saem_ordenados_por_slug(registry: ModuleRegistry) -> None:
    registry.register(ModuloDeTeste)
    registry.register(ModuloQueFalhaAoNascer)

    assert registry.slugs() == ["modulo-de-teste", "modulo-que-falha"]
    assert [classe.slug for classe in registry.all()] == registry.slugs()


def test_describe_tolera_falha_no_ui_e_registra_o_erro(registry: ModuleRegistry) -> None:
    class ModuloComUiQuebrada(ModuloDeTeste):
        """Building block cujo `ui()` estoura: o descritor nao pode sumir por isso."""

        slug = "modulo-ui-quebrada"

        def ui(self) -> UIDescriptor:
            """Estoura de proposito."""
            raise RuntimeError("template ausente")

    registry.register(ModuloComUiQuebrada)

    (descritor,) = registry.describe()

    assert descritor.slug == "modulo-ui-quebrada"
    assert descritor.ui == UIDescriptor(), "sem UI valida, o descritor sai com a UI padrao"
    assert any(origem == "modulo-ui-quebrada.ui" for origem, _ in registry.discover_errors)


def test_clear_esvazia_classes_instancias_e_erros(registry: ModuleRegistry) -> None:
    registry.register(ModuloDeTeste)
    registry.instantiate("modulo-de-teste")
    registry.discover_errors.append(("origem", "motivo"))

    registry.clear()

    assert len(registry) == 0
    assert registry.slugs() == []
    assert registry.discover_errors == []


# --------------------------------------------------------------------------- #
# Descoberta por entry points
# --------------------------------------------------------------------------- #
def _instalar_entry_points(monkeypatch: pytest.MonkeyPatch, *pontos: _EntryPointFalso) -> None:
    """Substitui `importlib.metadata.entry_points` pelos dubles informados.

    O alvo e o modulo `lukato.modules.registry` resolvido por `import_module`: o
    pacote `lukato.modules` reexporta o **singleton** com o mesmo nome, e um
    `from ... import registry` traria o objeto, nao o modulo.
    """
    modulo_registry = importlib.import_module("lukato.modules.registry")

    def falso(group: str = "", **kwargs: Any) -> list[_EntryPointFalso]:
        return [ponto for ponto in pontos if ponto.group == group]

    monkeypatch.setattr(modulo_registry.metadata, "entry_points", falso)


def test_discover_registra_os_entry_points_saudaveis(
    registry: ModuleRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _instalar_entry_points(monkeypatch, _EntryPointFalso("bom", resultado=ModuloDeTeste))

    registrados = registry.discover()

    assert registrados == 1
    assert registry.get("modulo-de-teste") is ModuloDeTeste
    assert registry.source_of("modulo-de-teste") == "entry_point"
    assert registry.discover_errors == []


def test_entry_point_quebrado_nao_derruba_a_descoberta_e_acumula_em_discover_errors(
    registry: ModuleRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _instalar_entry_points(
        monkeypatch,
        _EntryPointFalso("quebrado", erro=ImportError("modulo externo ausente")),
        _EntryPointFalso("bom", resultado=ModuloDeTeste),
    )

    registrados = registry.discover()

    assert registrados == 1, "o entry point saudavel continua sendo registrado"
    assert len(registry.discover_errors) == 1
    origem, motivo = registry.discover_errors[0]
    assert origem == f"{DEFAULT_ENTRY_POINT_GROUP}:quebrado"
    assert "ImportError" in motivo
    assert "modulo externo ausente" in motivo


def test_entry_point_que_carrega_classe_invalida_tambem_vira_erro_registrado(
    registry: ModuleRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _instalar_entry_points(monkeypatch, _EntryPointFalso("ruim", resultado=ModuloComSlugInvalido))

    registrados = registry.discover()

    assert registrados == 0
    assert len(registry.discover_errors) == 1
    assert "ValidationError" in registry.discover_errors[0][1]


# --------------------------------------------------------------------------- #
# Modulos embutidos
# --------------------------------------------------------------------------- #
def test_load_builtin_registra_os_cinco_building_blocks_embutidos(
    registry: ModuleRegistry,
) -> None:
    registrados = registry.load_builtin()

    assert registrados == len(BUILTIN_MODULE_NAMES) == 5
    assert registry.slugs() == ["adwatch", "auth", "finops", "knowledge", "processing"]
    assert registry.discover_errors == [], (
        f"nenhum embutido pode falhar ao carregar: {registry.discover_errors}"
    )


def test_load_builtin_e_idempotente(registry: ModuleRegistry) -> None:
    registry.load_builtin()

    segunda_carga = registry.load_builtin()

    assert segunda_carga == 0, "recarregar nao registra de novo o que ja esta la"
    assert len(registry) == 5


def test_describe_dos_embutidos_traz_kind_e_class_path_de_cada_um(
    builtin_registry: ModuleRegistry,
) -> None:
    descritores = {descritor.slug: descritor for descritor in builtin_registry.describe()}

    assert descritores["auth"].kind is ModuleKind.AUTH
    assert descritores["processing"].kind is ModuleKind.AGENT
    assert descritores["finops"].kind is ModuleKind.FINOPS
    assert descritores["knowledge"].kind is ModuleKind.KNOWLEDGE
    assert descritores["adwatch"].kind is ModuleKind.PIPELINE
    for descritor in descritores.values():
        assert descritor.class_path.startswith("lukato.modules.builtin.")


def test_repr_do_registry_resume_modulos_e_erros(registry: ModuleRegistry) -> None:
    registry.register(ModuloDeTeste)

    assert repr(registry) == "ModuleRegistry(modules=1, errors=0)"
