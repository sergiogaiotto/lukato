"""Registry de building blocks: descoberta, registro e instanciacao.

O nucleo nao conhece nenhum modulo em tempo de compilacao. Modulos chegam por
tres caminhos: o decorator :func:`register_module`, os embutidos carregados por
nome em :meth:`ModuleRegistry.load_builtin` e os externos publicados no grupo de
entry points ``lukato.modules``.
"""

from __future__ import annotations

import importlib
import logging
import re
from importlib import metadata
from typing import Literal

from pydantic import Field

from lukato.domain.errors import (
    ConflictError,
    ModuleError,
    ModuleNotFound,
    ValidationError,
)
from lukato.domain.models.base import DomainModel
from lukato.domain.models.module import MODULE_SLUG_PATTERN, ModuleBinding, ModuleKind
from lukato.domain.types import Json
from lukato.modules.base import BaseModule, UIDescriptor

__all__ = [
    "BUILTIN_MODULE_NAMES",
    "BUILTIN_PACKAGE",
    "DEFAULT_ENTRY_POINT_GROUP",
    "ModuleDescriptor",
    "ModuleRegistry",
    "ModuleSource",
    "register_module",
    "registry",
]

_logger = logging.getLogger(__name__)

ModuleSource = Literal["builtin", "entry_point"]
"""Origem de um modulo registrado."""

DEFAULT_ENTRY_POINT_GROUP = "lukato.modules"
"""Grupo de entry points varrido por :meth:`ModuleRegistry.discover`."""

BUILTIN_PACKAGE = "lukato.modules.builtin"
"""Pacote onde vivem os modulos embutidos."""

BUILTIN_MODULE_NAMES: tuple[str, ...] = (
    "auth_module",
    "processing_module",
    "finops_module",
    "knowledge_module",
    "adwatch_module",
)
"""Modulos embutidos carregados por nome, na ordem de boot."""

_SLUG_RE = re.compile(MODULE_SLUG_PATTERN)


def _class_path(module_cls: type[object]) -> str:
    """Caminho importavel completo de uma classe (`pacote.modulo:Classe`)."""
    return f"{module_cls.__module__}:{module_cls.__qualname__}"


class ModuleDescriptor(DomainModel):
    """Retrato serializavel de um building block registrado."""

    slug: str
    name: str
    kind: ModuleKind
    version: str = "1.0.0"
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    config_schema: Json = Field(default_factory=dict)
    default_binding: ModuleBinding = Field(default_factory=ModuleBinding)
    source: ModuleSource = "builtin"
    class_path: str = ""
    ui: UIDescriptor = Field(default_factory=UIDescriptor)


class ModuleRegistry:
    """Mapa `slug -> classe` de building blocks, com cache de instancias.

    Singleton de processo (:data:`registry`); `clear()` devolve o isolamento
    necessario aos testes.
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[BaseModule]] = {}
        self._sources: dict[str, ModuleSource] = {}
        self._instances: dict[str, BaseModule] = {}
        self.discover_errors: list[tuple[str, str]] = []

    # -- registro ----------------------------------------------------------
    def register(
        self,
        module_cls: type[BaseModule],
        *,
        replace: bool = False,
        source: ModuleSource = "builtin",
    ) -> type[BaseModule]:
        """Registra a classe pelo seu `slug` e devolve a propria classe.

        Slug ja ocupado por **outra** classe levanta :class:`ConflictError`, salvo
        `replace=True`. Registrar a **mesma** classe de novo e no-op: os cinco
        embutidos sao alcancaveis por dois caminhos (`load_builtin()` e o entry
        point `lukato.modules` do `pyproject.toml`), e com o pacote instalado os
        dois rodam. Sem esta idempotencia, o segundo caminho acumularia cinco
        erros em `discover_errors` e `/readyz` reportaria o registry degradado em
        toda instalacao empacotada — sem nada de errado acontecendo de fato.
        """
        slug = self._validate_class(module_cls)
        current = self._classes.get(slug)
        if current is module_cls:
            self._sources.setdefault(slug, source)
            return module_cls
        if current is not None and not replace:
            raise ConflictError(
                f"Slug de modulo ja registrado: '{slug}' (por {_class_path(current)}).",
                details={
                    "slug": slug,
                    "registered": _class_path(current),
                    "candidate": _class_path(module_cls),
                },
            )
        self._classes[slug] = module_cls
        self._sources[slug] = source
        self._instances.pop(slug, None)
        _logger.debug("Modulo registrado: slug=%s source=%s", slug, source)
        return module_cls

    def unregister(self, slug: str) -> None:
        """Remove o modulo do registro; slug desconhecido levanta `ModuleNotFound`."""
        if slug not in self._classes:
            raise ModuleNotFound(
                f"Modulo nao registrado: '{slug}'.",
                details={"slug": slug, "available": sorted(self._classes)},
            )
        del self._classes[slug]
        self._sources.pop(slug, None)
        self._instances.pop(slug, None)

    def clear(self) -> None:
        """Esvazia classes, instancias e erros de descoberta (usado em testes)."""
        self._classes.clear()
        self._sources.clear()
        self._instances.clear()
        self.discover_errors.clear()

    # -- consulta ----------------------------------------------------------
    def get(self, slug: str) -> type[BaseModule]:
        """Devolve a classe registrada para o slug; ausente levanta `ModuleNotFound`."""
        try:
            return self._classes[slug]
        except KeyError:
            raise ModuleNotFound(
                f"Modulo '{slug}' nao registrado.",
                details={"slug": slug, "available": sorted(self._classes)},
            ) from None

    def instantiate(self, slug: str) -> BaseModule:
        """Devolve a instancia unica do modulo (cache por slug)."""
        cached = self._instances.get(slug)
        if cached is not None:
            return cached
        module_cls = self.get(slug)
        try:
            instance = module_cls()
        except Exception as exc:
            raise ModuleError(
                f"Falha ao instanciar o modulo '{slug}': {exc}",
                details={"slug": slug, "class_path": _class_path(module_cls)},
            ) from exc
        self._instances[slug] = instance
        return instance

    def all(self) -> list[type[BaseModule]]:
        """Todas as classes registradas, ordenadas por slug."""
        return [module_cls for _, module_cls in sorted(self._classes.items())]

    def slugs(self) -> list[str]:
        """Slugs registrados, em ordem alfabetica."""
        return sorted(self._classes)

    def source_of(self, slug: str) -> ModuleSource:
        """Origem do modulo registrado (`builtin` ou `entry_point`)."""
        self.get(slug)
        return self._sources.get(slug, "builtin")

    def describe(self) -> list[ModuleDescriptor]:
        """Descritores completos de todos os modulos, ordenados por slug."""
        return [self._describe_one(slug, cls) for slug, cls in sorted(self._classes.items())]

    def __contains__(self, slug: object) -> bool:
        return isinstance(slug, str) and slug in self._classes

    def __len__(self) -> int:
        return len(self._classes)

    def __repr__(self) -> str:
        return f"ModuleRegistry(modules={len(self._classes)}, errors={len(self.discover_errors)})"

    # -- descoberta --------------------------------------------------------
    def discover(self, entry_point_group: str = DEFAULT_ENTRY_POINT_GROUP) -> int:
        """Registra modulos publicados em um grupo de entry points.

        Entry point quebrado nao derruba a descoberta: gera WARNING, entra em
        `discover_errors` e a varredura segue. Devolve quantos foram registrados.
        """
        registered = 0
        for entry_point in metadata.entry_points(group=entry_point_group):
            origin = f"{entry_point_group}:{entry_point.name}"
            try:
                loaded = entry_point.load()
                self.register(loaded, source="entry_point")
            except Exception as exc:
                self._record_error(origin, exc)
                continue
            registered += 1
        return registered

    def load_builtin(self) -> int:
        """Importa por nome os modulos embutidos e devolve quantos foram registrados.

        Modulo ainda inexistente nao e erro fatal: gera WARNING, entra em
        `discover_errors` e o boot segue.
        """
        registered = 0
        for module_name in BUILTIN_MODULE_NAMES:
            qualified = f"{BUILTIN_PACKAGE}.{module_name}"
            before = set(self._classes)
            try:
                self._import_builtin(qualified)
            except Exception as exc:
                self._record_error(qualified, exc)
            registered += len(set(self._classes) - before)
        return registered

    # -- internos ----------------------------------------------------------
    def _import_builtin(self, qualified: str) -> None:
        """Importa um modulo embutido e registra as classes que ele define."""
        imported = importlib.import_module(qualified)
        for candidate in vars(imported).values():
            if not self._is_registrable(candidate, qualified):
                continue
            if self._classes.get(candidate.slug) is candidate:
                continue  # ja registrado pelo decorator @register_module
            self.register(candidate, source="builtin")

    @staticmethod
    def _is_registrable(candidate: object, qualified: str) -> bool:
        """True para classes concretas de building block definidas nesse modulo."""
        if not isinstance(candidate, type) or not issubclass(candidate, BaseModule):
            return False
        if candidate is BaseModule or candidate.__module__ != qualified:
            return False
        if getattr(candidate, "__abstractmethods__", frozenset()):
            return False
        return bool(getattr(candidate, "slug", ""))

    def _validate_class(self, module_cls: type[BaseModule]) -> str:
        """Aplica as regras de registro e devolve o slug validado."""
        if not isinstance(module_cls, type) or not issubclass(module_cls, BaseModule):
            raise ValidationError(
                f"Modulo invalido: {module_cls!r} nao e subclasse de BaseModule.",
                details={"candidate": repr(module_cls)},
            )
        if module_cls is BaseModule:
            raise ValidationError(
                "BaseModule e abstrato e nao pode ser registrado.",
                details={"candidate": _class_path(module_cls)},
            )

        slug = getattr(module_cls, "slug", "")
        if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
            raise ValidationError(
                f"Slug de modulo invalido: {slug!r} nao casa com {MODULE_SLUG_PATTERN}.",
                details={
                    "slug": slug,
                    "pattern": MODULE_SLUG_PATTERN,
                    "class_path": _class_path(module_cls),
                },
            )

        kind = getattr(module_cls, "kind", None)
        try:
            ModuleKind(str(kind))
        except ValueError as exc:
            raise ValidationError(
                f"Kind de modulo invalido em '{slug}': {kind!r}.",
                details={
                    "slug": slug,
                    "kind": str(kind),
                    "allowed": [item.value for item in ModuleKind],
                },
            ) from exc

        if module_cls.handle is BaseModule.handle:
            raise ValidationError(
                f"Modulo '{slug}' nao implementa `handle`.",
                details={"slug": slug, "class_path": _class_path(module_cls)},
            )
        pending = sorted(getattr(module_cls, "__abstractmethods__", frozenset()))
        if pending:
            raise ValidationError(
                f"Modulo '{slug}' deixou metodos abstratos sem implementacao: {pending}.",
                details={"slug": slug, "missing": pending},
            )
        return slug

    def _describe_one(self, slug: str, module_cls: type[BaseModule]) -> ModuleDescriptor:
        """Monta o descritor de um modulo, tolerando falha no `ui()`."""
        ui = UIDescriptor()
        try:
            ui = self.instantiate(slug).ui()
        except Exception as exc:
            self._record_error(f"{slug}.ui", exc)
        return ModuleDescriptor(
            slug=slug,
            name=getattr(module_cls, "name", "") or slug,
            kind=ModuleKind(module_cls.kind),
            version=getattr(module_cls, "version", "1.0.0"),
            description=getattr(module_cls, "description", ""),
            capabilities=list(getattr(module_cls, "capabilities", ())),
            config_schema=dict(getattr(module_cls, "config_schema", {}) or {}),
            default_binding=getattr(module_cls, "default_binding", None) or ModuleBinding(),
            source=self._sources.get(slug, "builtin"),
            class_path=_class_path(module_cls),
            ui=ui,
        )

    def _record_error(self, origin: str, exc: BaseException) -> None:
        """Guarda a falha em `discover_errors` e emite WARNING sem interromper."""
        reason = f"{type(exc).__name__}: {exc}"
        self.discover_errors.append((origin, reason))
        _logger.warning("Modulo nao carregado: origem=%s motivo=%s", origin, reason)


registry = ModuleRegistry()
"""Registry singleton do processo."""


def register_module(cls: type[BaseModule]) -> type[BaseModule]:
    """Decorator: registra a classe no :data:`registry` e devolve a propria classe."""
    return registry.register(cls)
