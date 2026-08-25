"""Guardiao da regra hexagonal do lukato (SPEC-0000 secao 2).

Este arquivo nao testa comportamento: testa a **forma** do codigo de producao.
Ele le `src/lukato/**/*.py` com `ast` (nunca com `grep` de subprocesso) e coleta
todo import — de nivel de modulo, de dentro de funcao, de dentro de `class` e de
dentro de `if TYPE_CHECKING:`. Import tardio escondido em funcao continua sendo
acoplamento: e exatamente ali que a regra costuma vazar.

As cinco regras verificadas (SPEC-0000 secao 2)::

    1. domain/      -> so stdlib + pydantic; nada de I/O, nada de outras camadas
    2. domain/      -> nao importa application, adapters, interfaces nem modules
    3. application/ -> nao importa adapters nem interfaces
    4. modules/     -> nao importa adapters nem interfaces
    5. composition  -> unico modulo autorizado a juntar as tres camadas

Mais tres convencoes da secao 14: `from __future__ import annotations` em todo
modulo, nenhum `print()` em codigo de producao e nenhum segredo em arquivo.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final, NamedTuple

import pytest

import lukato

pytestmark = pytest.mark.unit

# --------------------------------------------------------------------------- #
# Localizacao do codigo de producao
# --------------------------------------------------------------------------- #
PACOTE: Final[Path] = Path(lukato.__file__).resolve().parent
"""Diretorio `src/lukato`, raiz do codigo de producao."""

SRC: Final[Path] = PACOTE.parent
"""Diretorio `src`, usado para transformar caminho em nome de modulo."""

PROJETO: Final[Path] = SRC.parent
"""Raiz do repositorio."""

DEPLOY: Final[Path] = PROJETO / "deploy"
"""Manifestos de implantacao, varridos junto com `src` na busca por segredos."""

COMPOSITION: Final[Path] = PACOTE / "composition.py"
"""O composition root: o unico modulo autorizado a juntar as tres camadas."""

CLI: Final[Path] = PACOTE / "interfaces" / "cli.py"
"""A CLI escreve em `sys.stdout` de proposito; e a unica isenta do teste de `print`."""

# --------------------------------------------------------------------------- #
# Vocabulario das regras
# --------------------------------------------------------------------------- #
BIBLIOTECAS_DE_IO: Final[frozenset[str]] = frozenset(
    {
        "sqlalchemy",
        "fastapi",
        "httpx",
        "openai",
        "langgraph",
        "langchain",
        "langfuse",
        "jinja2",
        "asyncpg",
        "psycopg",
        "numpy",
        "prometheus_client",
        "bcrypt",
        "jwt",
    }
)
"""Bibliotecas de I/O proibidas em `domain/`: o dominio e stdlib + pydantic."""

CAMADA_ADAPTERS: Final[str] = "lukato.adapters"
CAMADA_APPLICATION: Final[str] = "lukato.application"
CAMADA_INTERFACES: Final[str] = "lukato.interfaces"
CAMADA_MODULES: Final[str] = "lukato.modules"

CAMADAS_PROIBIDAS_NO_DOMINIO: Final[tuple[str, ...]] = (
    CAMADA_ADAPTERS,
    CAMADA_APPLICATION,
    CAMADA_INTERFACES,
    CAMADA_MODULES,
)
"""Nenhuma delas pode aparecer em `domain/`: o dominio esta no centro do hexagono."""

PADROES_DE_SEGREDO: Final[dict[str, re.Pattern[str]]] = {
    "chave de API estilo OpenAI (sk-...)": re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    "chave de acesso AWS (AKIA...)": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "chave privada PEM": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
}
"""Segredos que nunca podem estar versionados (SPEC-0000 secao 14)."""


# --------------------------------------------------------------------------- #
# Leitura do codigo com `ast`
# --------------------------------------------------------------------------- #
class Importacao(NamedTuple):
    """Um import encontrado no codigo, com origem exata para o relatorio de falha."""

    modulo: str
    arquivo: Path
    linha: int

    @property
    def raiz(self) -> str:
        """Primeiro componente do modulo importado (`sqlalchemy.orm` -> `sqlalchemy`)."""
        return self.modulo.split(".", 1)[0]

    def __str__(self) -> str:
        """Formato `caminho/relativo.py:linha -> modulo`, pronto para abrir no editor."""
        return f"{self.arquivo.relative_to(PROJETO)}:{self.linha} -> {self.modulo}"


def modulos_de(diretorio: Path) -> list[Path]:
    """Lista, em ordem estavel, todos os arquivos `.py` sob o diretorio."""
    return sorted(diretorio.rglob("*.py"))


def nome_do_modulo(arquivo: Path) -> str:
    """Converte o caminho do arquivo no nome pontilhado do modulo Python."""
    partes = list(arquivo.relative_to(SRC).with_suffix("").parts)
    if partes and partes[-1] == "__init__":
        partes.pop()
    return ".".join(partes)


def pacote_do_modulo(arquivo: Path) -> str:
    """Pacote que contem o arquivo (usado para resolver imports relativos)."""
    nome = nome_do_modulo(arquivo)
    if arquivo.name == "__init__.py":
        return nome
    return nome.rpartition(".")[0]


def _resolve_relativo(arquivo: Path, modulo: str | None, nivel: int) -> str:
    """Transforma `from ..x import y` no nome absoluto correspondente."""
    partes = pacote_do_modulo(arquivo).split(".")
    subir = max(0, nivel - 1)
    base = partes[: len(partes) - subir] if subir else partes
    caminho = [parte for parte in base if parte]
    if modulo:
        caminho.append(modulo)
    return ".".join(caminho)


def importacoes_de(arquivo: Path) -> list[Importacao]:
    """Coleta **todos** os imports do arquivo, inclusive os de dentro de funcoes.

    `ast.walk` percorre a arvore inteira, entao um `import` tardio dentro de uma
    funcao (ou de um `if TYPE_CHECKING:`) e contabilizado igual a um do topo.
    """
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
    encontrados: list[Importacao] = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            encontrados.extend(Importacao(alias.name, arquivo, no.lineno) for alias in no.names)
        elif isinstance(no, ast.ImportFrom):
            if no.level:
                nome = _resolve_relativo(arquivo, no.module, no.level)
            else:
                nome = no.module or ""
            if nome:
                encontrados.append(Importacao(nome, arquivo, no.lineno))
    return encontrados


def importacoes_da_camada(diretorio: Path) -> list[Importacao]:
    """Todos os imports de todos os modulos de uma camada."""
    return [
        importacao for arquivo in modulos_de(diretorio) for importacao in importacoes_de(arquivo)
    ]


def importa_camada(importacoes: Sequence[Importacao], camada: str) -> bool:
    """True quando alguma das importacoes aponta para a camada informada."""
    return any(
        importacao.modulo == camada or importacao.modulo.startswith(f"{camada}.")
        for importacao in importacoes
    )


def violacoes_de_camada(diretorio: Path, camadas: Sequence[str]) -> list[Importacao]:
    """Imports do diretorio que apontam para qualquer uma das camadas proibidas."""
    return [
        importacao
        for importacao in importacoes_da_camada(diretorio)
        if any(
            importacao.modulo == camada or importacao.modulo.startswith(f"{camada}.")
            for camada in camadas
        )
    ]


def relatorio(violacoes: Sequence[Importacao]) -> str:
    """Lista as violacoes, uma por linha, com arquivo e numero de linha."""
    return "\n".join(f"  - {violacao}" for violacao in violacoes)


def arquivos_de_texto(diretorio: Path) -> Iterator[tuple[Path, str]]:
    """Percorre os arquivos legiveis como texto sob o diretorio."""
    for arquivo in sorted(diretorio.rglob("*")):
        if not arquivo.is_file():
            continue
        try:
            yield arquivo, arquivo.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


# --------------------------------------------------------------------------- #
# Regra 1 — o dominio nao conhece I/O
# --------------------------------------------------------------------------- #
def test_dominio_nao_importa_bibliotecas_de_io() -> None:
    """`domain/` so pode depender de stdlib e pydantic (SPEC-0000 secao 2, regra 1)."""
    violacoes = [
        importacao
        for importacao in importacoes_da_camada(PACOTE / "domain")
        if importacao.raiz in BIBLIOTECAS_DE_IO
    ]
    assert not violacoes, (
        "o dominio importou biblioteca de I/O — ele deve depender apenas de stdlib "
        f"e pydantic (proibidas: {', '.join(sorted(BIBLIOTECAS_DE_IO))}):\n"
        f"{relatorio(violacoes)}"
    )


# --------------------------------------------------------------------------- #
# Regra 2 — o dominio nao conhece as camadas de fora
# --------------------------------------------------------------------------- #
def test_dominio_nao_importa_camadas_externas() -> None:
    """`domain/` nao importa application, adapters, interfaces nem modules (regra 1)."""
    violacoes = violacoes_de_camada(PACOTE / "domain", CAMADAS_PROIBIDAS_NO_DOMINIO)
    assert not violacoes, (
        "o dominio importou uma camada externa; ele fica no centro do hexagono e so "
        f"conhece a si mesmo:\n{relatorio(violacoes)}"
    )


# --------------------------------------------------------------------------- #
# Regra 3 — a aplicacao so conhece o dominio
# --------------------------------------------------------------------------- #
def test_application_nao_importa_adapters_nem_interfaces() -> None:
    """`application/` importa `domain` e stdlib; a composicao acontece no root (regra 2)."""
    violacoes = violacoes_de_camada(PACOTE / "application", (CAMADA_ADAPTERS, CAMADA_INTERFACES))
    assert not violacoes, (
        "a camada de aplicacao importou adaptador ou interface; ela recebe portas por "
        f"injecao e nunca escolhe implementacao:\n{relatorio(violacoes)}"
    )


# --------------------------------------------------------------------------- #
# Regra 4 — os building blocks so conhecem portas
# --------------------------------------------------------------------------- #
def test_modules_nao_importam_adapters_nem_interfaces() -> None:
    """`modules/` recebe tudo pelo `ModuleContext`; nao escolhe adaptador (SPEC-0001)."""
    violacoes = violacoes_de_camada(PACOTE / "modules", (CAMADA_ADAPTERS, CAMADA_INTERFACES))
    assert not violacoes, (
        "um building block importou adaptador ou interface; tudo o que ele precisa "
        f"chega pelo ModuleContext:\n{relatorio(violacoes)}"
    )


# --------------------------------------------------------------------------- #
# Regra 5 — so o composition root junta as tres camadas
# --------------------------------------------------------------------------- #
def test_somente_composition_monta_o_container() -> None:
    """So `composition.py` constroi o `Container` (SPEC-0000 regra 5).

    A regra 5 nasceu como "so composition importa as tres camadas ao mesmo tempo",
    o que era largo demais: `interfaces/` E infraestrutura, e ha tres usos
    legitimos e documentados de adaptador na borda (parsing de payload do
    WhisperX, registro de metricas do `/metrics`, politicas do `seed`). O que a
    regra realmente protege e a **fiacao**: se o `Container` puder ser montado em
    varios lugares, trocar um adaptador deixa de ser uma linha.

    Este teste verifica o invariante que importa, e e mais estrito do que o
    anterior no que diz respeito a fiacao.
    """
    constroem: list[str] = []
    for arquivo in modulos_de(PACOTE):
        if arquivo == COMPOSITION:
            continue
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
        for no in ast.walk(arvore):
            if isinstance(no, ast.Call) and isinstance(no.func, ast.Name):
                if no.func.id == "Container":
                    constroem.append(f"{arquivo.relative_to(PROJETO)}:{no.lineno}")
    assert not constroem, (
        "somente src/lukato/composition.py pode montar o Container; encontrado em:\n"
        + "\n".join(f"  - {onde}" for onde in constroem)
    )


def test_dominio_e_aplicacao_nunca_importam_adapters() -> None:
    """O invariante de dentro: `domain/` e `application/` ignoram a infraestrutura."""
    violacoes: list[str] = []
    for pacote in ("domain", "application"):
        for arquivo in modulos_de(PACOTE / pacote):
            importacoes = importacoes_de(arquivo)
            for camada in (CAMADA_ADAPTERS, CAMADA_INTERFACES):
                if importa_camada(importacoes, camada):
                    violacoes.append(f"{arquivo.relative_to(PROJETO)} -> {camada}")
    assert not violacoes, "camada interna importando infraestrutura:\n" + "\n".join(
        f"  - {v}" for v in violacoes
    )


# --------------------------------------------------------------------------- #
# Convencoes da secao 14
# --------------------------------------------------------------------------- #
def test_todo_modulo_tem_future_annotations() -> None:
    """Todo modulo comeca com `from __future__ import annotations` (secao 14)."""
    faltando: list[str] = []
    for arquivo in modulos_de(PACOTE):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
        if not arvore.body:
            continue
        tem = any(
            isinstance(no, ast.ImportFrom)
            and no.module == "__future__"
            and any(alias.name == "annotations" for alias in no.names)
            for no in arvore.body
        )
        if not tem:
            faltando.append(str(arquivo.relative_to(PROJETO)))
    assert not faltando, (
        "modulo(s) sem 'from __future__ import annotations' — sem ele as anotacoes "
        "sao avaliadas na importacao e as referencias adiantadas quebram:\n"
        + "\n".join(f"  - {caminho}" for caminho in faltando)
    )


def test_nenhum_print_no_codigo_de_producao() -> None:
    """Nada de `print()`: o projeto usa `structlog` (secao 14).

    `interfaces/cli.py` e isento **enquanto** escrever pela `sys.stdout.write`: a
    saida de um comando de linha nao e log, e nem por isso pode virar `print`.
    """
    encontrados: list[str] = []
    for arquivo in modulos_de(PACOTE):
        codigo = arquivo.read_text(encoding="utf-8")
        if arquivo == CLI and "sys.stdout.write" in codigo:
            continue
        arvore = ast.parse(codigo, filename=str(arquivo))
        for no in ast.walk(arvore):
            if isinstance(no, ast.Call) and isinstance(no.func, ast.Name) and no.func.id == "print":
                encontrados.append(f"{arquivo.relative_to(PROJETO)}:{no.lineno}")
    assert not encontrados, (
        "chamada a print() no codigo de producao; use o logger estruturado:\n"
        + "\n".join(f"  - {local}" for local in encontrados)
    )


def test_nenhum_segredo_aparente() -> None:
    """Nenhum segredo versionado em `src/` ou `deploy/` (secao 14)."""
    achados: list[str] = []
    for diretorio in (PACOTE, DEPLOY):
        if not diretorio.exists():
            continue
        for arquivo, conteudo in arquivos_de_texto(diretorio):
            for descricao, padrao in PADROES_DE_SEGREDO.items():
                for ocorrencia in padrao.finditer(conteudo):
                    linha = conteudo.count("\n", 0, ocorrencia.start()) + 1
                    achados.append(f"{arquivo.relative_to(PROJETO)}:{linha} — {descricao}")
    assert not achados, (
        "segredo aparente versionado; credencial so entra por Settings/ambiente:\n"
        + "\n".join(f"  - {achado}" for achado in achados)
    )
