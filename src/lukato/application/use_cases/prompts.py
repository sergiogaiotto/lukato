"""Casos de uso da biblioteca de prompts versionados (SPEC-0000 secao 6.2).

A biblioteca guarda **versoes**, nao rascunhos mutaveis. Alterar o `template` de um
prompt jamais sobrescreve o texto que ja esta em producao: :class:`UpdatePrompt`
percebe a mudanca, grava uma **nova versao** (`version + 1`, `is_active=True`) e
**desativa** a versao anterior. Os demais campos (`name`, `description`, `role`,
`labels`, `is_active`) sao metadados da propria versao e mudam no lugar.

A consequencia pratica e o contrato da SPEC-0003: `binding.system_prompt_id` pode
apontar para uma versao congelada, enquanto `GetPromptBySlug` sempre devolve a
versao vigente e `ListPromptVersions` preserva a auditoria de tudo que ja rodou.

`PreviewPrompt` alimenta o preview do console (SPEC-0009, rota `/prompts`):
renderiza com o que houver, devolve `{"rendered", "missing"}` e **nunca levanta**
por variavel faltando — a lacuna continua visivel como `{{ variavel }}` no texto.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, Final, TypeVar, cast

from lukato.application.container import Container
from lukato.application.dto import (
    DEFAULT_PAGE_LIMIT,
    UNSET,
    Maybe,
    Page,
    PageRequest,
    is_set,
)
from lukato.application.use_cases.modules import authorize
from lukato.config import get_logger
from lukato.domain.errors import ConflictError, NotFoundError, ValidationError
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.models.prompt import PromptRole, PromptTemplate, extract_variables
from lukato.domain.ports.unit_of_work import UnitOfWork
from lukato.domain.types import Id, Json, slugify, utcnow

__all__ = [
    "FIRST_VERSION",
    "ClonePromptVersion",
    "CreatePrompt",
    "DeletePrompt",
    "GetPrompt",
    "GetPromptBySlug",
    "ListPromptVersions",
    "ListPrompts",
    "PreviewPrompt",
    "PromptCreateInput",
    "PromptFilter",
    "PromptUpdateInput",
    "UpdatePrompt",
]

_logger = get_logger(__name__)

_T = TypeVar("_T")

FIRST_VERSION: Final[int] = 1
"""Numero da primeira versao de um slug recem-criado."""


# ---------------------------------------------------------------------------
# Utilitarios internos
# ---------------------------------------------------------------------------
def _coerce(data: Any, factory: type[_T], *, what: str) -> _T:
    """Aceita o DTO ja montado ou o objeto JSON cru vindo da borda HTTP/UI.

    A borda traduz o corpo da requisicao; permitir o mapa evita que cada
    interface precise conhecer o dataclass, sem abrir mao da validacao — chave
    desconhecida vira :class:`ValidationError` em vez de ser ignorada em silencio.
    """
    if isinstance(data, factory):
        return data
    if isinstance(data, Mapping):
        known = {item.name for item in fields(cast(Any, factory))}
        unknown = sorted(str(key) for key in data if str(key) not in known)
        if unknown:
            raise ValidationError(
                f"Campos desconhecidos em {what}: {', '.join(unknown)}.",
                details={"unknown": unknown, "supported": sorted(known)},
            )
        return factory(**{str(key): value for key, value in data.items()})
    raise ValidationError(
        f"{what} deve ser um objeto JSON ou um {factory.__name__}.",
        details={"received": type(data).__name__},
    )


def _gap(name: str) -> str:
    """Marcador de variavel ausente mantido no texto do preview."""
    return "{{ " + name + " }}"


def _require_template(template: str) -> str:
    """Exige um template com conteudo; texto em branco nao e prompt."""
    if template and template.strip():
        return template
    raise ValidationError(
        "O prompt precisa de um 'template' com conteudo.",
        details={"field": "template"},
    )


def _declared_variables(template: str, declared: Sequence[str]) -> list[str]:
    """Normaliza a lista informada exigindo que ela cubra todo o template.

    Uma declaracao que esquece um placeholder mentiria para a UI e so quebraria na
    hora de renderizar; a divergencia e recusada aqui, com as faltantes em
    `details["missing"]`.
    """
    names = [str(name).strip() for name in declared if str(name).strip()]
    missing = [name for name in extract_variables(template) if name not in names]
    if missing:
        raise ValidationError(
            f"As variaveis {', '.join(missing)} aparecem no template mas nao foram declaradas.",
            details={"missing": missing, "declared": names},
        )
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return list(seen)


def _resolve_variables(
    declared: Maybe[Sequence[str] | None],
    *,
    template: str,
    current: Sequence[str] = (),
    template_changed: bool = True,
) -> list[str]:
    """Decide a lista de variaveis gravada na versao.

    Lista informada -> respeitada (e conferida contra o template); vazia, `None`
    ou ausente -> extraida do template; template intacto e nada informado -> a
    lista atual, que pode conter variaveis opcionais declaradas a mao.
    """
    if is_set(declared) and declared:
        return _declared_variables(template, declared)
    if not is_set(declared) and not template_changed and current:
        return list(current)
    return extract_variables(template)


async def _find_prompt(uow: UnitOfWork, reference: str) -> PromptTemplate | None:
    """Resolve o prompt por slug (versao vigente), por id e, por fim, no historico."""
    candidate = (reference or "").strip()
    if not candidate:
        return None
    active = await uow.prompts.get_by_slug(candidate)
    if active is not None:
        return active
    by_id = await uow.prompts.get(candidate)
    if by_id is not None:
        return by_id
    versions = await uow.prompts.list_versions(candidate)
    return versions[0] if versions else None


async def _require_prompt(uow: UnitOfWork, reference: str) -> PromptTemplate:
    """Resolve o prompt ou levanta :class:`NotFoundError`."""
    found = await _find_prompt(uow, reference)
    if found is None:
        raise NotFoundError(
            f"Prompt '{reference}' nao encontrado.",
            details={"reference": reference},
        )
    return found


async def _next_version(uow: UnitOfWork, slug: str) -> int:
    """Proximo numero livre de versao do slug (1 quando o slug e novo)."""
    versions = await uow.prompts.list_versions(slug)
    return max((version.version for version in versions), default=FIRST_VERSION - 1) + 1


async def _deactivate_siblings(uow: UnitOfWork, slug: str, *, keep: Id) -> list[int]:
    """Desativa as demais versoes do slug; devolve os numeros desativados.

    Garante o invariante da biblioteca: no maximo **uma** versao ativa por slug,
    que e a que `GetPromptBySlug` (e portanto o binding de um modulo) resolve.
    """
    retired: list[int] = []
    for version in await uow.prompts.list_versions(slug):
        if version.id == keep or not version.is_active:
            continue
        await uow.prompts.update(
            version.model_copy(update={"is_active": False, "updated_at": utcnow()})
        )
        retired.append(version.version)
    return retired


# ---------------------------------------------------------------------------
# DTOs de entrada
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PromptCreateInput:
    """Dados de criacao da **primeira versao** de um slug de prompt."""

    slug: str
    name: str = ""
    description: str = ""
    role: PromptRole = PromptRole.SYSTEM
    template: str = ""
    variables: Sequence[str] | None = None
    labels: Sequence[str] = ()
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class PromptUpdateInput:
    """Atualizacao parcial de um prompt; campos ausentes ficam :data:`UNSET`.

    Informar `template` com texto diferente do vigente **cria uma nova versao**;
    os demais campos alteram a versao atual.
    """

    name: Maybe[str] = UNSET
    description: Maybe[str] = UNSET
    role: Maybe[PromptRole] = UNSET
    template: Maybe[str] = UNSET
    variables: Maybe[Sequence[str] | None] = UNSET
    labels: Maybe[Sequence[str]] = UNSET
    is_active: Maybe[bool] = UNSET

    def changes(self) -> Json:
        """Mapa `campo -> valor` apenas com o que foi efetivamente informado."""
        candidates: dict[str, Maybe[Any]] = {
            "name": self.name,
            "description": self.description,
            "role": self.role,
            "template": self.template,
            "labels": self.labels,
            "is_active": self.is_active,
        }
        changed: Json = {}
        for name, value in candidates.items():
            if not is_set(value):
                continue
            changed[name] = list(value) if name == "labels" else value
        return changed


@dataclass(frozen=True, slots=True)
class PromptFilter:
    """Filtros de listagem da biblioteca de prompts."""

    search: str | None = None
    is_active: bool | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        """Normaliza a janela de paginacao pelos limites normativos."""
        window = PageRequest(limit=self.limit, offset=self.offset)
        object.__setattr__(self, "limit", window.limit)
        object.__setattr__(self, "offset", window.offset)

    @property
    def page(self) -> PageRequest:
        """Janela de paginacao correspondente a este filtro."""
        return PageRequest(limit=self.limit, offset=self.offset)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class _PromptUseCase:
    """Base dos casos de uso de prompt: guarda o `Container` injetado."""

    __slots__ = ("_container",)

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        """Container de dependencias desta aplicacao."""
        return self._container


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
class CreatePrompt(_PromptUseCase):
    """Cria a versao 1 de um slug de prompt."""

    async def execute(
        self, data: PromptCreateInput | Mapping[str, Any], principal: Principal
    ) -> PromptTemplate:
        """Grava a primeira versao; slug ja existente levanta :class:`ConflictError`.

        Quando `variables` nao e informada, ela e **extraida do template**: o
        editor da UI nao precisa manter a lista a mao.
        """
        authorize(principal, Permission.PROMPT_WRITE, "criar prompts")
        payload = _coerce(data, PromptCreateInput, what="a criacao de prompt")
        slug = slugify(payload.slug or payload.name)
        template = _require_template(payload.template)
        prompt = PromptTemplate(
            slug=slug,
            name=payload.name or slug,
            description=payload.description,
            role=payload.role,
            template=template,
            variables=_resolve_variables(payload.variables, template=template),
            version=FIRST_VERSION,
            is_active=payload.is_active,
            labels=list(payload.labels),
        )
        async with self._container.uow_factory() as uow:
            if await uow.prompts.list_versions(slug):
                raise ConflictError(
                    f"Ja existe o prompt '{slug}'; use UpdatePrompt para versionar o template.",
                    details={"slug": slug},
                )
            created = await uow.prompts.add(prompt)
            await uow.commit()
        _logger.info(
            "prompt_created",
            slug=created.slug,
            version=created.version,
            variables=len(created.variables),
        )
        return created


class GetPrompt(_PromptUseCase):
    """Busca um prompt por identificador ou por slug."""

    async def execute(self, reference: str, principal: Principal) -> PromptTemplate:
        """Devolve o prompt; ausente levanta :class:`NotFoundError`."""
        authorize(principal, Permission.PROMPT_READ, "ler prompts")
        async with self._container.uow_factory() as uow:
            return await _require_prompt(uow, reference)


class GetPromptBySlug(_PromptUseCase):
    """Busca a versao vigente de um slug — ou uma versao especifica do historico."""

    async def execute(
        self, slug: str, principal: Principal, *, version: int | None = None
    ) -> PromptTemplate:
        """Sem `version` devolve a versao ativa; com `version` devolve aquela versao."""
        authorize(principal, Permission.PROMPT_READ, "ler prompts")
        async with self._container.uow_factory() as uow:
            if version is None:
                return await _require_prompt(uow, slug)
            for candidate in await uow.prompts.list_versions(slug):
                if candidate.version == version:
                    return candidate
        raise NotFoundError(
            f"O prompt '{slug}' nao tem a versao {version}.",
            details={"slug": slug, "version": version},
        )


class ListPrompts(_PromptUseCase):
    """Lista prompts paginados, com busca textual e filtro de atividade.

    O repositorio devolve **todas** as versoes; filtrar por `is_active=True` da a
    visao de catalogo (uma linha por slug), que e o que a UI mostra por padrao.
    """

    async def execute(
        self, filters: PromptFilter | Mapping[str, Any], principal: Principal
    ) -> Page[PromptTemplate]:
        """Devolve a pagina no formato normativo `items/total/limit/offset`."""
        authorize(principal, Permission.PROMPT_READ, "listar prompts")
        criteria = _coerce(filters, PromptFilter, what="o filtro de prompts")
        selection: Json = {}
        if criteria.search:
            selection["search"] = criteria.search
        if criteria.is_active is not None:
            selection["is_active"] = criteria.is_active
        async with self._container.uow_factory() as uow:
            items = await uow.prompts.list(
                **selection, limit=criteria.limit, offset=criteria.offset
            )
            total = await uow.prompts.count(**selection)
        return Page(items=list(items), total=total, limit=criteria.limit, offset=criteria.offset)


class UpdatePrompt(_PromptUseCase):
    """Atualiza um prompt — **versionando** quando o template muda.

    Regra normativa desta biblioteca: `template` diferente do vigente nunca
    sobrescreve a versao em uso. O caso de uso grava uma nova `PromptTemplate`
    com `version + 1` e desativa a anterior, de modo que qualquer execucao ja
    auditada continue apontando para o texto exato que rodou. Metadados
    (`name`, `description`, `role`, `labels`, `is_active`) mudam na propria versao.
    """

    async def execute(
        self,
        reference: str,
        data: PromptUpdateInput | Mapping[str, Any],
        principal: Principal,
    ) -> PromptTemplate:
        """Aplica a atualizacao e devolve a versao resultante (nova ou a mesma)."""
        authorize(principal, Permission.PROMPT_WRITE, "alterar prompts")
        payload = _coerce(data, PromptUpdateInput, what="a atualizacao de prompt")
        changes = payload.changes()
        async with self._container.uow_factory() as uow:
            current = await _require_prompt(uow, reference)
            if not changes and not is_set(payload.variables):
                return current
            template = _require_template(str(changes.get("template", current.template)))
            template_changed = template != current.template
            variables = _resolve_variables(
                payload.variables,
                template=template,
                current=current.variables,
                template_changed=template_changed,
            )
            if template_changed:
                stored = await self._new_version(uow, current, changes, template, variables)
            else:
                stored = await self._same_version(uow, current, changes, variables)
            await uow.commit()
        return stored

    @staticmethod
    async def _new_version(
        uow: UnitOfWork,
        current: PromptTemplate,
        changes: Json,
        template: str,
        variables: list[str],
    ) -> PromptTemplate:
        """Grava a proxima versao do slug e aposenta a anterior."""
        activate = bool(changes.get("is_active", True))
        created = await uow.prompts.add(
            PromptTemplate(
                slug=current.slug,
                name=str(changes.get("name", current.name)),
                description=str(changes.get("description", current.description)),
                role=PromptRole(changes.get("role", current.role)),
                template=template,
                variables=variables,
                version=await _next_version(uow, current.slug),
                is_active=activate,
                labels=list(changes.get("labels", current.labels)),
            )
        )
        retired = await _deactivate_siblings(uow, current.slug, keep=created.id) if activate else []
        _logger.info(
            "prompt_versioned",
            slug=created.slug,
            version=created.version,
            previous=current.version,
            retired=retired,
        )
        return created

    @staticmethod
    async def _same_version(
        uow: UnitOfWork, current: PromptTemplate, changes: Json, variables: list[str]
    ) -> PromptTemplate:
        """Atualiza metadados da versao vigente, preservando o texto em producao."""
        stored = await uow.prompts.update(
            current.model_copy(update={**changes, "variables": variables, "updated_at": utcnow()})
        )
        if stored.is_active and not current.is_active:
            await _deactivate_siblings(uow, stored.slug, keep=stored.id)
        _logger.info(
            "prompt_updated",
            slug=stored.slug,
            version=stored.version,
            fields=sorted(changes),
        )
        return stored


class DeletePrompt(_PromptUseCase):
    """Remove uma versao de prompt — ou o slug inteiro."""

    async def execute(
        self, reference: str, principal: Principal, *, all_versions: bool = False
    ) -> int:
        """Apaga a versao resolvida (ou todas as do slug) e devolve quantas sairam."""
        authorize(principal, Permission.PROMPT_WRITE, "remover prompts")
        async with self._container.uow_factory() as uow:
            target = await _require_prompt(uow, reference)
            doomed = await uow.prompts.list_versions(target.slug) if all_versions else [target]
            for prompt in doomed:
                await uow.prompts.delete(prompt.id)
            await uow.commit()
        _logger.info("prompt_deleted", slug=target.slug, versions=len(doomed))
        return len(doomed)


class ListPromptVersions(_PromptUseCase):
    """Lista o historico completo de um slug, da versao mais recente para a mais antiga."""

    async def execute(self, slug: str, principal: Principal) -> list[PromptTemplate]:
        """Devolve as versoes do slug; slug inexistente levanta :class:`NotFoundError`."""
        authorize(principal, Permission.PROMPT_READ, "ler prompts")
        async with self._container.uow_factory() as uow:
            versions = list(await uow.prompts.list_versions((slug or "").strip()))
        if not versions:
            raise NotFoundError(
                f"Prompt '{slug}' nao encontrado.",
                details={"slug": slug},
            )
        return sorted(versions, key=lambda prompt: prompt.version, reverse=True)


# ---------------------------------------------------------------------------
# Preview e clonagem
# ---------------------------------------------------------------------------
class PreviewPrompt(_PromptUseCase):
    """Renderiza um prompt com as variaveis informadas, **sem levantar**.

    E o motor do preview do console: uma variavel faltando e informacao para o
    editor, nao erro. O texto volta renderizado com as lacunas preservadas como
    `{{ variavel }}` e a lista do que falta em `missing`.
    """

    async def execute(
        self,
        slug: str,
        variables: Json | None,
        principal: Principal,
        *,
        template: str | None = None,
    ) -> Json:
        """Devolve `{"rendered", "missing", ...}` para o slug ou para um texto avulso.

        `template` permite pre-visualizar um rascunho ainda nao salvo: nesse caso
        nada e lido do repositorio.
        """
        authorize(principal, Permission.PROMPT_READ, "pre-visualizar prompts")
        provided = dict(variables or {})
        if template is not None:
            draft = PromptTemplate(
                slug=slugify(slug or "preview"),
                name="preview",
                template=_require_template(template),
                version=FIRST_VERSION,
            )
            return _preview(draft, provided, persisted=False)
        async with self._container.uow_factory() as uow:
            prompt = await _require_prompt(uow, slug)
        return _preview(prompt, provided, persisted=True)


def _preview(prompt: PromptTemplate, provided: Json, *, persisted: bool) -> Json:
    """Monta o resultado do preview preenchendo as lacunas com o proprio placeholder."""
    required = extract_variables(prompt.template)
    missing = [name for name in required if name not in provided]
    filled: Json = {**provided, **{name: _gap(name) for name in missing}}
    return {
        "slug": prompt.slug,
        "version": prompt.version,
        "role": prompt.role.value,
        "rendered": prompt.render(filled),
        "missing": missing,
        "variables": required,
        "unused": sorted(name for name in provided if name not in required),
        "complete": not missing,
        "persisted": persisted,
    }


class ClonePromptVersion(_PromptUseCase):
    """Duplica uma versao de prompt como nova versao — do mesmo slug ou de outro."""

    async def execute(
        self,
        reference: str,
        principal: Principal,
        *,
        target_slug: str | None = None,
        name: str | None = None,
        activate: bool = True,
    ) -> PromptTemplate:
        """Copia o texto e os metadados da origem para a proxima versao livre do destino.

        Sem `target_slug` o clone vira a proxima versao do mesmo slug (o jeito de
        editar a partir de uma versao antiga); com `target_slug` nasce um prompt
        novo, comecando na versao 1 quando o destino ainda nao existe.
        """
        authorize(principal, Permission.PROMPT_WRITE, "clonar prompts")
        async with self._container.uow_factory() as uow:
            source = await _require_prompt(uow, reference)
            slug = slugify(target_slug) if target_slug else source.slug
            clone = PromptTemplate(
                slug=slug,
                name=name or source.name,
                description=source.description,
                role=source.role,
                template=source.template,
                variables=list(source.variables),
                version=await _next_version(uow, slug),
                is_active=activate,
                labels=list(source.labels),
            )
            created = await uow.prompts.add(clone)
            if activate:
                await _deactivate_siblings(uow, slug, keep=created.id)
            await uow.commit()
        _logger.info(
            "prompt_cloned",
            slug=created.slug,
            version=created.version,
            source_slug=source.slug,
            source_version=source.version,
        )
        return created
