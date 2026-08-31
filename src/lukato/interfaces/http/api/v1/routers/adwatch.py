"""Rotas de `/api/v1/adwatch` — catalogo de comerciais, midia e deteccao temporal.

O AdWatch responde a uma pergunta operacional precisa: *este comercial foi ao ar
nesta midia, quando comecou, quando terminou e com qual evidencia?* (SPEC-0010).
Esta borda expoe as tres superficies do recurso — o catalogo (`/commercials`), os
ativos de midia e os seus artefatos (`/media`) e o resultado do funil
(`/detections`) — mais o inventario de capacidades (`/capabilities`).

Tres pontos do contrato desta borda merecem explicacao:

* **o parsing dos artefatos acontece aqui, na interface.** `POST /media/{id}/
  transcript`, `/scenes` e `/ocr` recebem JSON cru em formatos que variam com a
  ferramenta que o produziu (WhisperX, PySceneDetect, PaddleOCR) e o convertem em
  modelos de dominio com os importadores de
  :mod:`lukato.adapters.media.importers`. A camada de aplicacao recebe listas ja
  tipadas — `list[TranscriptWord]`, `list[SceneCut]`, `list[OcrText]` —, o que
  mantem o caso de uso ignorante de formato de arquivo. E a fronteira definida
  pela SPEC-0010 secao 3.1, e o motivo de este ser o unico router que importa
  `adapters`;
* **os tres endpoints de importacao aceitam corpo JSON *ou* upload de arquivo.**
  A saida do WhisperX de uma gravacao longa chega quase sempre como arquivo, e
  obrigar o operador a cola-la no corpo da requisicao seria hostil sem nenhum
  ganho. O `Content-Type` decide o caminho: `multipart/form-data` le o primeiro
  arquivo enviado, qualquer outro le o corpo direto. O conteudo em si e o mesmo
  JSON nos dois casos;
* **importar transcricao substitui a anterior e destrava a deteccao.** Sem linha
  do tempo de palavras nao existe janela deslizante: `POST /media/{id}/detect`
  responde `422` com a instrucao de importar ou ingerir antes, nunca um relatorio
  vazio que pareceria "nenhum comercial encontrado".

As permissoes vem de `lukato.application.use_cases.adwatch`
(`ADWATCH_READ`/`ADWATCH_WRITE`/`ADWATCH_RUN`) em vez de serem redeclaradas aqui:
a checagem da borda precisa ser **exatamente** a mesma que o caso de uso faz
depois, ou o papel `operator` passaria em uma e seria recusado na outra.

Nenhuma rota toca repositorio: toda operacao passa por um caso de uso de
:mod:`lukato.application.use_cases.adwatch`, construido com o `Container`
injetado por :func:`lukato.interfaces.http.deps.get_container`.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from typing import Annotated, Any, BinaryIO, Final
from uuid import uuid4

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi import (
    UploadFile as ApiUploadFile,
)
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from lukato.adapters.media.factory import CAPABILITY_HINTS, capability_report
from lukato.adapters.media.importers import OcrImporter, SceneImporter, TranscriptImporter
from lukato.application.use_cases.adwatch import (
    ADWATCH_READ,
    ADWATCH_RUN,
    ADWATCH_WRITE,
    BulkImportCommercials,
    CommercialFilter,
    CreateCommercial,
    DeleteCommercial,
    DetectCommercials,
    DetectionFilter,
    GetCommercial,
    GetDetection,
    GetMedia,
    GetMediaCapabilities,
    ImportOcr,
    ImportScenes,
    ImportTranscript,
    IngestMedia,
    ListCommercials,
    ListDetections,
    ListMedia,
    MediaFilter,
    MediaInput,
    RegisterMedia,
    ReviewDetection,
    UpdateCommercial,
)
from lukato.domain.errors import ValidationError
from lukato.domain.models.adwatch import DetectionStatus, MediaKind
from lukato.domain.models.identity import Principal
from lukato.domain.types import Id, Json
from lukato.interfaces.http.deps import ContainerDep, PaginationDep, require
from lukato.interfaces.http.schemas.adwatch import (
    BulkImportRequest,
    BulkImportResponse,
    CapabilitiesOut,
    CommercialCreate,
    CommercialDetailOut,
    CommercialOut,
    CommercialUpdate,
    DetectionOut,
    DetectionReportOut,
    DetectionReviewRequest,
    DetectRequest,
    ImportResultOut,
    IngestReportOut,
    MediaCreate,
    MediaOut,
    OcrImportRequest,
    SceneImportRequest,
    TranscriptImportRequest,
)
from lukato.interfaces.http.schemas.common import OutSchema, Page, error_responses

__all__ = [
    "AdWatchCapabilitiesOut",
    "CapabilityOut",
    "MediaArtifactsOut",
    "MediaDetailOut",
    "router",
]

router = APIRouter(prefix="/adwatch", tags=["adwatch"])
"""Roteador do AdWatch, sob `/api/v1/adwatch` (SPEC-0000 secao 11)."""


# ---------------------------------------------------------------------------
# Dependencias de identidade e parametros de caminho
# ---------------------------------------------------------------------------
_Reader = Annotated[Principal, Depends(require(ADWATCH_READ))]
"""Principal que ja provou poder ler catalogo, midia e deteccoes."""

_Writer = Annotated[Principal, Depends(require(ADWATCH_WRITE))]
"""Principal que ja provou poder escrever catalogo, midia, artefatos e revisoes."""

_Runner = Annotated[Principal, Depends(require(ADWATCH_RUN))]
"""Principal que ja provou poder disparar as etapas caras (ingestao e deteccao)."""

_CommercialId = Annotated[
    str,
    Path(
        min_length=1,
        description="Identificador interno (`Commercial.id`) ou codigo de negocio (`COM_000234`).",
    ),
]
"""Referencia do comercial recebida no caminho da rota."""

_MediaId = Annotated[
    str, Path(min_length=1, description="Identificador do ativo de midia (`MediaAsset.id`).")
]
"""Referencia do ativo de midia recebida no caminho da rota."""

_DetectionId = Annotated[
    str, Path(min_length=1, description="Identificador da deteccao (`Detection.id`).")
]
"""Referencia da deteccao recebida no caminho da rota."""

_LIST_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403)
"""Erros das rotas de listagem, que nao resolvem nenhum identificador."""

_ITEM_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403, 404)
"""Erros das rotas que resolvem uma entidade existente."""

_WRITE_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403, 404, 409, 422)
"""Erros das rotas de escrita, que validam corpo e podem colidir com o catalogo."""

_IMPORT_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403, 404, 413, 422)
"""Erros das rotas de importacao de artefatos (corpo cru, possivelmente grande)."""


# ---------------------------------------------------------------------------
# Schemas proprios desta borda
# ---------------------------------------------------------------------------
class MediaArtifactsOut(OutSchema):
    """O que ja existe gravado para um ativo de midia.

    A contagem importa mais que o conteudo: e ela que diz se `POST /detect` tem
    linha do tempo suficiente para rodar e quais sinais estarao disponiveis na
    fusao de score.
    """

    transcript: bool = Field(default=False, description="Se ha transcricao gravada.")
    transcript_words: int = Field(default=0, ge=0, description="Palavras da transcricao.")
    transcript_source: str | None = Field(
        default=None, description="Origem da transcricao: `whisperx` ou `import`."
    )
    scene_cuts: int = Field(default=0, ge=0, description="Cortes de cena disponiveis.")
    ocr_texts: int = Field(default=0, ge=0, description="Textos de OCR disponiveis.")
    detections: int = Field(default=0, ge=0, description="Deteccoes ja gravadas para o ativo.")


class MediaDetailOut(OutSchema):
    """Detalhe de `GET /media/{id}`: o ativo, os artefatos e as capacidades."""

    media: MediaOut
    artifacts: MediaArtifactsOut = Field(default_factory=MediaArtifactsOut)
    capabilities: dict[str, bool] = Field(
        default_factory=dict, description="Adaptadores multimodais disponiveis nesta instalacao."
    )

    @classmethod
    def from_result(cls, report: Json) -> MediaDetailOut:
        """Converte o mapa devolvido por `GetMedia.detail`."""
        return cls.model_validate(report)


class CapabilityOut(OutSchema):
    """Uma capacidade multimodal: se esta disponivel e como habilita-la."""

    name: str = Field(description="Chave da capacidade: probe, asr, ocr, scenes ou vision.")
    label: str = Field(description="Nome legivel da capacidade, para o console.")
    available: bool = Field(description="Se o adaptador respondeu que esta pronto para uso.")
    adapter: str | None = Field(default=None, description="Classe do adaptador instalado.")
    hint: str = Field(description="O que instalar ou configurar para habilitar a capacidade.")
    detail: str = Field(description="Frase pronta em portugues explicando a situacao atual.")


class AdWatchCapabilitiesOut(CapabilitiesOut):
    """Resposta de `GET /capabilities`: limiares vigentes **e** o que falta instalar.

    `CapabilitiesOut` ja traz o mapa `nome -> disponivel` e a configuracao do
    funil; `details` acrescenta, por capacidade, o adaptador encontrado e a
    instrucao de habilitacao. Sem essa instrucao a resposta so sabe dizer
    "ausente", e o operador fica sem o proximo passo.
    """

    details: list[CapabilityOut] = Field(
        default_factory=list, description="Situacao de cada capacidade, na ordem do pipeline."
    )

    @classmethod
    def of(cls, report: Json, detailed: Json) -> AdWatchCapabilitiesOut:
        """Combina o relatorio do caso de uso com o detalhamento dos adaptadores."""
        entries: list[CapabilityOut] = []
        for name in _CAPABILITY_ORDER:
            item = detailed.get(name) or {}
            available = bool(item.get("available", False))
            adapter = item.get("adapter")
            hint = str(item.get("hint") or CAPABILITY_HINTS.get(name, ""))
            label = _CAPABILITY_LABELS[name]
            entries.append(
                CapabilityOut(
                    name=name,
                    label=label,
                    available=available,
                    adapter=str(adapter) if adapter else None,
                    hint=hint,
                    detail=_capability_detail(
                        label, available=available, adapter=adapter, hint=hint
                    ),
                )
            )
        return cls(**report, details=entries)


_CAPABILITY_ORDER: Final[tuple[str, ...]] = ("probe", "asr", "ocr", "scenes", "vision")
"""Capacidades na ordem em que o pipeline as usa (SPEC-0010 secao 2)."""

_CAPABILITY_LABELS: Final[dict[str, str]] = {
    "probe": "sondagem de midia e extracao de audio (FFmpeg)",
    "asr": "transcricao automatica com marcacao de palavra (WhisperX)",
    "ocr": "leitura do texto em tela (PaddleOCR)",
    "scenes": "deteccao de cortes e fades (PySceneDetect)",
    "vision": "juiz multimodal do fim do funil (Qwen VLM)",
}
"""Nome legivel de cada capacidade, exibido no console."""


def _capability_detail(label: str, *, available: bool, adapter: Any, hint: str) -> str:
    """Monta a frase em portugues que explica a situacao da capacidade."""
    if available:
        via = f" via {adapter}" if adapter else ""
        return f"Disponivel: {label}{via}."
    return f"Indisponivel: {label}. Para habilitar, {hint}."


# ---------------------------------------------------------------------------
# Leitura do corpo das importacoes (JSON direto ou upload de arquivo)
# ---------------------------------------------------------------------------
_FORM_CONTENT_TYPES: Final[tuple[str, ...]] = (
    "multipart/form-data",
    "application/x-www-form-urlencoded",
)
"""Tipos de conteudo que chegam como formulario e carregam o JSON em um arquivo."""

MAX_IMPORT_BYTES: Final[int] = 64 * 1024 * 1024
"""Teto do corpo de uma importacao: 64 MiB cobrem horas de transcricao WhisperX."""

_UPLOAD_FIELD_DESCRIPTION: Final[str] = (
    "Arquivo `.json` com o mesmo conteudo aceito no corpo `application/json`."
)


def _import_request_body(model: type[BaseModel], *, description: str) -> Json:
    """Documenta no OpenAPI as duas formas aceitas: corpo JSON e upload de arquivo.

    O corpo e lido a mao (o `Content-Type` decide o caminho), entao o FastAPI nao
    tem como deduzir o `requestBody` a partir da assinatura da rota. Declara-lo
    aqui mantem o Swagger honesto — inclusive com o botao de upload.
    """
    return {
        "required": True,
        "description": description,
        "content": {
            "application/json": {"schema": model.model_json_schema()},
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": _UPLOAD_FIELD_DESCRIPTION,
                        }
                    },
                    "required": ["file"],
                }
            },
        },
    }


async def _import_payload(request: Request, *, what: str) -> Json | list[Any]:
    """Le o corpo da importacao, venha ele como JSON direto ou como arquivo."""
    media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    raw = (
        await _uploaded_bytes(request, what=what)
        if media_type in _FORM_CONTENT_TYPES
        else await request.body()
    )
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"O arquivo de {what} tem {len(raw)} bytes e excede o limite de "
                f"{MAX_IMPORT_BYTES} bytes por importacao. Divida a midia em partes."
            ),
        )
    if not raw.strip():
        raise ValidationError(
            f"O corpo da importacao de {what} veio vazio: envie o JSON no corpo da "
            "requisicao ou um arquivo `.json` em `multipart/form-data`.",
            details={"what": what, "content_type": media_type},
        )
    return _decode_json(raw, what=what)


async def _uploaded_bytes(request: Request, *, what: str) -> bytes:
    """Extrai o conteudo do primeiro arquivo (ou campo textual) do formulario."""
    form = await request.form()
    try:
        for value in form.values():
            if isinstance(value, UploadFile):
                return await value.read()
        for value in form.values():
            if isinstance(value, str) and value.strip():
                return value.encode("utf-8")
    finally:
        await form.close()
    raise ValidationError(
        f"Nenhum arquivo foi enviado na importacao de {what}: anexe o JSON em um "
        "campo de arquivo (por exemplo `file`) ou envie o corpo como "
        "`application/json`.",
        details={"what": what, "fields": sorted(form.keys())},
    )


def _decode_json(raw: bytes, *, what: str) -> Json | list[Any]:
    """Desserializa o JSON da importacao apontando exatamente onde ele quebrou."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValidationError(
            f"O arquivo de {what} nao esta em UTF-8: {exc.reason}.",
            details={"what": what, "position": exc.start},
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"O corpo da importacao de {what} nao e JSON valido: {exc.msg} "
            f"(linha {exc.lineno}, coluna {exc.colno}).",
            details={"what": what, "line": exc.lineno, "column": exc.colno},
        ) from exc
    if not isinstance(parsed, (dict, list)):
        raise ValidationError(
            f"A importacao de {what} espera uma lista ou um objeto JSON, e nao "
            f"{type(parsed).__name__}.",
            details={"what": what, "received": type(parsed).__name__},
        )
    return parsed


# ---------------------------------------------------------------------------
# Capacidades (rota literal, declarada antes das rotas por identificador)
# ---------------------------------------------------------------------------
@router.get(
    "/capabilities",
    response_model=AdWatchCapabilitiesOut,
    status_code=status.HTTP_200_OK,
    responses=_LIST_ERRORS,
    summary="Capacidades multimodais instaladas",
    description=(
        "Diz o que esta instalado nesta maquina — FFmpeg, WhisperX, PaddleOCR, "
        "PySceneDetect e o juiz multimodal — e, para cada ausencia, exatamente o que "
        "instalar ou configurar para habilita-la. Traz tambem os pesos, os limiares e "
        "as janelas vigentes, alem do teto de score alcancavel sem OCR e sem juiz "
        "visual: um comercial nunca chega a 1.0 com uma modalidade faltando, e o "
        "console precisa explicar isso ao operador. A deteccao continua possivel com "
        "tudo ausente, pelo caminho de importacao de transcricao."
    ),
)
async def get_capabilities(container: ContainerDep, principal: _Reader) -> AdWatchCapabilitiesOut:
    """Devolve o inventario de capacidades com as instrucoes de habilitacao."""
    report = await GetMediaCapabilities(container).execute(principal)
    return AdWatchCapabilitiesOut.of(report, capability_report(container.media))


# ---------------------------------------------------------------------------
# Catalogo de comerciais
# ---------------------------------------------------------------------------
@router.post(
    "/commercials",
    response_model=CommercialOut,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 409, 422),
    summary="Cria comercial",
    description=(
        "Cadastra o comercial e constroi a sua `AdFingerprint` — texto normalizado, "
        "conjunto de tokens, palavras-chave de alto IDF, frases-chave e embedding — "
        "que e o que a deteccao compara contra as janelas da midia. `commercial_id` e "
        "o codigo de negocio e precisa ser unico: repeti-lo responde `409`."
    ),
)
async def create_commercial(
    payload: CommercialCreate, container: ContainerDep, principal: _Writer
) -> CommercialOut:
    """Cria o comercial e devolve a entidade gravada."""
    commercial = await CreateCommercial(container).execute(payload.to_input(), principal)
    return CommercialOut.from_domain(commercial)


@router.get(
    "/commercials",
    response_model=Page[CommercialOut],
    status_code=status.HTTP_200_OK,
    responses=_LIST_ERRORS,
    summary="Lista comerciais",
    description=(
        "Pagina o catalogo filtrando por texto livre (`search`, aplicado a codigo, "
        "marca, campanha e locucao), por `brand`, por `campaign` e por `is_active`. "
        "Somente comerciais ativos entram na deteccao, entao `is_active=false` e a "
        "forma de auditar o que foi tirado de circulacao sem apagar o historico."
    ),
)
async def list_commercials(
    container: ContainerDep,
    principal: _Reader,
    pagination: PaginationDep,
    search: Annotated[
        str | None, Query(description="Texto livre buscado em codigo, marca, campanha e locucao.")
    ] = None,
    brand: Annotated[str | None, Query(description="Restringe a uma marca anunciante.")] = None,
    campaign: Annotated[str | None, Query(description="Restringe a uma campanha.")] = None,
    is_active: Annotated[
        bool | None, Query(description="True lista apenas os comerciais em circulacao.")
    ] = None,
) -> Page[CommercialOut]:
    """Devolve a pagina do catalogo no envelope normativo da API."""
    filters = CommercialFilter(
        search=search,
        brand=brand,
        campaign=campaign,
        is_active=is_active,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    result = await ListCommercials(container).execute(filters, principal)
    return Page[CommercialOut].from_result(result, CommercialOut.from_domain)


@router.post(
    "/commercials/bulk",
    response_model=BulkImportResponse,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403, 422),
    summary="Importa comerciais em lote",
    description=(
        "Aceita um array puro de comerciais ou o objeto `{items, update_existing}`. "
        "Cada item e gravado na sua propria transacao: um codigo duplicado ou um texto "
        "invalido fica isolado em `skipped`/`errors` e nao derruba o resto do lote. Os "
        "embeddings sao pedidos uma unica vez, ao final, o que torna a importacao de "
        "centenas de pecas viavel em uma chamada. `update_existing=true` atualiza o "
        "comercial ja cadastrado em vez de pula-lo."
    ),
)
async def bulk_import_commercials(
    payload: BulkImportRequest, container: ContainerDep, principal: _Writer
) -> BulkImportResponse:
    """Importa o lote e devolve criados, atualizados, pulados e recusados."""
    result = await BulkImportCommercials(container).execute(
        payload.to_inputs(), principal, update_existing=payload.update_existing
    )
    return BulkImportResponse.from_result(result)


@router.get(
    "/commercials/{commercial_id}",
    response_model=CommercialDetailOut,
    status_code=status.HTTP_200_OK,
    responses=_ITEM_ERRORS,
    summary="Detalha um comercial",
    description=(
        "Devolve o comercial e a assinatura que a deteccao usa contra ele. A rota "
        "resolve tanto o identificador interno quanto o codigo de negocio "
        "(`COM_000234`), porque o operador conhece o segundo e a UI carrega o primeiro. "
        "`fingerprint: null` significa catalogo gravado antes da assinatura existir — "
        "salvar o comercial de novo a reconstroi."
    ),
)
async def get_commercial(
    container: ContainerDep, principal: _Reader, commercial_id: _CommercialId
) -> CommercialDetailOut:
    """Devolve o comercial com a sua assinatura de matching."""
    detail = await GetCommercial(container).detail(commercial_id, principal)
    return CommercialDetailOut.from_result(detail)


@router.put(
    "/commercials/{commercial_id}",
    response_model=CommercialOut,
    status_code=status.HTTP_200_OK,
    responses=_WRITE_ERRORS,
    summary="Atualiza um comercial",
    description=(
        "Atualizacao parcial: apenas os campos enviados mudam. Alterar `text`, "
        "`keywords`, `key_phrases` ou `duration_expected` **regera a assinatura** — o "
        "que o matching procura passa a ser o texto novo a partir da proxima deteccao. "
        "Deteccoes ja gravadas nao sao reavaliadas: elas registram o que foi decidido "
        "com a assinatura da epoca."
    ),
)
async def update_commercial(
    payload: CommercialUpdate,
    container: ContainerDep,
    principal: _Writer,
    commercial_id: _CommercialId,
) -> CommercialOut:
    """Aplica as alteracoes e devolve o comercial atualizado."""
    commercial = await UpdateCommercial(container).execute(
        commercial_id, payload.to_input(), principal
    )
    return CommercialOut.from_domain(commercial)


@router.delete(
    "/commercials/{commercial_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_ITEM_ERRORS,
    summary="Remove um comercial",
    description=(
        "Apaga o comercial e, em cascata, a sua assinatura. Operacao destrutiva e sem "
        "corpo de resposta. Para tirar uma peca de circulacao preservando as deteccoes "
        "que ela ja explicou, prefira `PUT` com `is_active: false`."
    ),
)
async def delete_commercial(
    container: ContainerDep, principal: _Writer, commercial_id: _CommercialId
) -> Response:
    """Remove o comercial e responde 204 sem corpo."""
    await DeleteCommercial(container).execute(commercial_id, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Ativos de midia
# ---------------------------------------------------------------------------
@router.post(
    "/media",
    response_model=MediaOut,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 422),
    summary="Registra midia",
    description=(
        "Cria o ativo com `status: registered`, sem abrir o arquivo. Registrar e "
        "barato e nao exige FFmpeg: duracao e fps informados sao aceitos como estao e "
        "a ingestao, quando rodar, sobrescreve com o que a sondagem apurar. O `uri` "
        "pode ser um caminho local ou uma URL — quem sabe le-lo e o adaptador de "
        "sondagem, nao esta borda."
    ),
)
async def register_media(
    payload: MediaCreate, container: ContainerDep, principal: _Writer
) -> MediaOut:
    """Registra o ativo e devolve a entidade gravada."""
    asset = await RegisterMedia(container).execute(payload.to_input(), principal)
    return MediaOut.from_domain(asset)


_UPLOAD_SUBDIR: Final[str] = "uploads"
"""Subdiretorio de `adwatch.workdir` que recebe os uploads — dentro do volume."""

_UPLOAD_CHUNK_BYTES: Final[int] = 1024 * 1024
"""Bloco copiado por vez: 1 MiB mantem a memoria constante em arquivos de horas."""

_UPLOAD_EXTENSIONS: Final[dict[MediaKind, frozenset[str]]] = {
    MediaKind.VIDEO: frozenset({".avi", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm"}),
    MediaKind.AUDIO: frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}),
}
"""Extensoes aceitas por natureza — os contenedores que a sondagem FFmpeg sabe ler."""

_CARACTERE_FORA_DO_NOME: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")
"""Tudo que nao entra no nome gravado em disco vira `_`."""


class _UploadExcedeuOTeto(Exception):
    """O arquivo passou de `adwatch.upload_max_mb` no meio da copia."""


def _nome_seguro(original: str) -> str:
    """Reduz o nome enviado a um basename inofensivo.

    Navegadores mandam so o basename, mas o campo e conteudo do cliente: pode
    vir `C:\\pasta\\video.mp4`, `../../etc/cron.d/x` ou unicode de controle. O
    nome final participa do caminho gravado, entao aqui ele perde qualquer
    componente de diretorio e qualquer caractere fora de `[A-Za-z0-9._-]`.
    """
    base = pathlib.PurePosixPath(original.replace("\\", "/")).name
    limpo = _CARACTERE_FORA_DO_NOME.sub("_", base).strip("._")
    return (limpo or "midia")[-120:]


def _copiar_upload(origem: BinaryIO, destino: pathlib.Path, *, teto_bytes: int) -> int:
    """Copia o upload em blocos para `destino` e devolve o total de bytes.

    Roda em thread (I/O sincrono fora do event loop). Passar do teto levanta
    :class:`_UploadExcedeuOTeto` — quem chama apaga o parcial.
    """
    total = 0
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("wb") as saida:
        while bloco := origem.read(_UPLOAD_CHUNK_BYTES):
            total += len(bloco)
            if total > teto_bytes:
                raise _UploadExcedeuOTeto
            saida.write(bloco)
    return total


def _descartar_upload(destino: pathlib.Path) -> None:
    """Apaga o arquivo parcial sem reclamar se ele nem chegou a existir."""
    destino.unlink(missing_ok=True)


@router.post(
    "/media/upload",
    response_model=MediaOut,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 413, 422),
    summary="Envia um arquivo de midia e registra o ativo",
    description=(
        "O irmao hospedado do `POST /media`: em vez de anotar um caminho que o "
        "servidor ja enxerga, recebe o proprio arquivo em `multipart/form-data`, "
        "grava a copia em `<adwatch.workdir>/uploads` (dentro do volume da "
        "aplicacao) e registra o ativo apontando para ela — pronto para a "
        "ingestao. O teto e `adwatch.upload_max_mb`; a extensao precisa "
        "corresponder a natureza declarada em `kind`."
    ),
)
async def upload_media(
    container: ContainerDep,
    principal: _Writer,
    file: Annotated[ApiUploadFile, File(description="O arquivo de video ou audio.")],
    kind: Annotated[MediaKind, Form(description="Natureza do ativo.")] = MediaKind.VIDEO,
    title: Annotated[str, Form(description="Titulo exibido no console.")] = "",
) -> MediaOut:
    """Grava o arquivo enviado no armazenamento da aplicacao e registra o ativo."""
    original = file.filename or ""
    nome = _nome_seguro(original)
    extensao = pathlib.PurePosixPath(nome).suffix.lower()
    aceitas = _UPLOAD_EXTENSIONS[kind]
    if extensao not in aceitas:
        raise ValidationError(
            f"a extensao {extensao or '(nenhuma)'} nao corresponde a natureza "
            f"'{kind.value}'; aceitas: {', '.join(sorted(aceitas))}",
            details={"filename": original, "kind": kind.value},
        )

    ajustes = container.settings.adwatch
    teto_bytes = ajustes.upload_max_mb * 1024 * 1024
    destino = pathlib.Path(ajustes.workdir) / _UPLOAD_SUBDIR / f"{uuid4().hex}-{nome}"
    try:
        total = await asyncio.to_thread(_copiar_upload, file.file, destino, teto_bytes=teto_bytes)
    except _UploadExcedeuOTeto:
        await asyncio.to_thread(_descartar_upload, destino)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"O arquivo excede o teto de {ajustes.upload_max_mb} MiB do upload "
                "(`adwatch.upload_max_mb`). Aumente o limite ou registre a midia por "
                "caminho, com o arquivo ja acessivel ao servidor."
            ),
        ) from None
    except Exception:
        await asyncio.to_thread(_descartar_upload, destino)
        raise
    if total == 0:
        await asyncio.to_thread(_descartar_upload, destino)
        raise ValidationError(
            "o arquivo enviado esta vazio",
            details={"filename": original, "kind": kind.value},
        )

    entrada = MediaInput(
        uri=str(destino),
        kind=kind,
        title=title or pathlib.PurePosixPath(nome).stem,
        metadata={
            "upload": {
                "original_filename": original,
                "size_bytes": total,
                "content_type": file.content_type or "",
            }
        },
    )
    try:
        asset = await RegisterMedia(container).execute(entrada, principal)
    except Exception:
        # O registro falhou depois da copia: sem o ativo apontando para ele, o
        # arquivo seria um orfao invisivel ocupando o volume para sempre.
        await asyncio.to_thread(_descartar_upload, destino)
        raise
    return MediaOut.from_domain(asset)


@router.get(
    "/media",
    response_model=Page[MediaOut],
    status_code=status.HTTP_200_OK,
    responses=_LIST_ERRORS,
    summary="Lista ativos de midia",
    description=(
        "Pagina os ativos filtrando por `status` (`registered`, `ingested`, "
        "`analyzed`, `failed`) e por texto livre em `uri` e titulo. O `status` conta a "
        "historia do ativo: `registered` ainda nao tem linha do tempo, `ingested` ja "
        "tem transcricao e `analyzed` ja passou pelo funil de deteccao."
    ),
)
async def list_media(
    container: ContainerDep,
    principal: _Reader,
    pagination: PaginationDep,
    media_status: Annotated[
        str | None,
        Query(
            alias="status",
            description="Restringe a um estagio: registered, ingested, analyzed ou failed.",
        ),
    ] = None,
    search: Annotated[
        str | None, Query(description="Texto livre buscado no `uri` e no titulo do ativo.")
    ] = None,
) -> Page[MediaOut]:
    """Devolve a pagina de ativos no envelope normativo da API."""
    filters = MediaFilter(
        status=media_status, search=search, limit=pagination.limit, offset=pagination.offset
    )
    result = await ListMedia(container).execute(filters, principal)
    return Page[MediaOut].from_result(result, MediaOut.from_domain)


@router.get(
    "/media/{media_id}",
    response_model=MediaDetailOut,
    status_code=status.HTTP_200_OK,
    responses=_ITEM_ERRORS,
    summary="Detalha um ativo de midia",
    description=(
        "Devolve o ativo, o que ja existe gravado para ele — transcricao (com a "
        "origem e a contagem de palavras), cortes de cena, textos de OCR e deteccoes — "
        "e as capacidades multimodais instaladas. E a resposta que diz, antes de "
        "gastar processamento, se `POST /detect` tem linha do tempo para rodar e quais "
        "sinais entrarao na fusao de score."
    ),
)
async def get_media(
    container: ContainerDep, principal: _Reader, media_id: _MediaId
) -> MediaDetailOut:
    """Devolve o ativo com os artefatos disponiveis e as capacidades instaladas."""
    detail = await GetMedia(container).detail(media_id, principal)
    return MediaDetailOut.from_result(detail)


@router.post(
    "/media/{media_id}/ingest",
    response_model=IngestReportOut,
    status_code=status.HTTP_200_OK,
    responses=_ITEM_ERRORS,
    summary="Executa a ingestao possivel",
    description=(
        "Roda, nesta ordem, sondagem, extracao de audio, ASR, deteccao de cenas e OCR. "
        "**Cada etapa cujo adaptador nao esteja instalado e registrada e pulada** — "
        "nenhuma indisponibilidade derruba a ingestao, e o relatorio devolve "
        "exatamente o que foi alcancado, o que foi pulado e por que. Em uma instalacao "
        "sem FFmpeg e sem GPU a chamada responde `200` com todas as etapas em "
        "`skipped`, e o caminho para seguir e importar a transcricao pronta."
    ),
)
async def ingest_media(
    container: ContainerDep, principal: _Runner, media_id: _MediaId
) -> IngestReportOut:
    """Executa a ingestao e devolve o relatorio das etapas."""
    report = await IngestMedia(container).execute(media_id, principal)
    return IngestReportOut.from_result(report)


@router.post(
    "/media/{media_id}/transcript",
    response_model=ImportResultOut,
    status_code=status.HTTP_201_CREATED,
    responses=_IMPORT_ERRORS,
    summary="Importa transcricao",
    description=(
        'Aceita a lista simples (`[{"word", "start", "end"}]`), o objeto com '
        '`words` e o JSON completo do WhisperX (`{"segments": [{"words": [...]}]}`), '
        "no corpo `application/json` **ou** como arquivo em `multipart/form-data`. "
        "Substitui a transcricao anterior, estende a duracao conhecida do ativo e "
        "promove `registered` para `ingested`. **Este e o caminho que torna o pipeline "
        "inteiro executavel sem FFmpeg, sem GPU e sem rede** (SPEC-0010 secao 3.1)."
    ),
    openapi_extra={
        "requestBody": _import_request_body(
            TranscriptImportRequest,
            description="Transcricao em JSON (lista de palavras, objeto WhisperX ou arquivo).",
        )
    },
)
async def import_transcript(
    request: Request, container: ContainerDep, principal: _Writer, media_id: _MediaId
) -> ImportResultOut:
    """Parseia a transcricao na borda e grava as palavras ja tipadas."""
    payload = TranscriptImportRequest.model_validate(
        await _import_payload(request, what="transcricao")
    )
    words = TranscriptImporter.parse(payload.payload)
    transcript = await ImportTranscript(container).execute(
        media_id, words, principal, language=payload.language, source=payload.source
    )
    return ImportResultOut(
        media_id=transcript.media_id, imported=len(transcript.words), kind="transcript"
    )


@router.post(
    "/media/{media_id}/scenes",
    response_model=ImportResultOut,
    status_code=status.HTTP_201_CREATED,
    responses=_IMPORT_ERRORS,
    summary="Importa cortes de cena",
    description=(
        'Aceita a lista de cortes (`[{"index", "start", "end", "kind"}]`) ou o '
        "objeto que a embrulha em `scenes`/`cuts`, no corpo `application/json` **ou** "
        "como arquivo. Substitui os cortes anteriores. Os cortes nao produzem "
        "deteccao: eles refinam a fronteira da deteccao ja decidida, encaixando "
        "`start` e `end` no corte mais proximo — e o que leva o erro de inicio e fim "
        "para baixo de dois segundos."
    ),
    openapi_extra={
        "requestBody": _import_request_body(
            SceneImportRequest, description="Cortes de cena em JSON (lista, objeto ou arquivo)."
        )
    },
)
async def import_scenes(
    request: Request, container: ContainerDep, principal: _Writer, media_id: _MediaId
) -> ImportResultOut:
    """Parseia os cortes na borda e grava os `SceneCut` ja tipados."""
    payload = SceneImportRequest.model_validate(
        await _import_payload(request, what="cortes de cena")
    )
    scenes = SceneImporter.parse(payload.payload)
    imported = await ImportScenes(container).execute(media_id, scenes, principal)
    return ImportResultOut(media_id=media_id, imported=imported, kind="scenes")


@router.post(
    "/media/{media_id}/ocr",
    response_model=ImportResultOut,
    status_code=status.HTTP_201_CREATED,
    responses=_IMPORT_ERRORS,
    summary="Importa textos de OCR",
    description=(
        'Aceita a lista de textos (`[{"text", "start", "end", "confidence"}]`) '
        "ou o objeto que a embrulha em `ocr`/`texts`, no corpo `application/json` "
        "**ou** como arquivo. Substitui os textos anteriores. O OCR e o sinal que "
        "reconhece a peca pelo letreiro quando a locucao foi cortada ou abafada; sem "
        "ele o `ocr_match` da fusao fica em zero e o teto de score cai."
    ),
    openapi_extra={
        "requestBody": _import_request_body(
            OcrImportRequest, description="Textos de OCR em JSON (lista, objeto ou arquivo)."
        )
    },
)
async def import_ocr(
    request: Request, container: ContainerDep, principal: _Writer, media_id: _MediaId
) -> ImportResultOut:
    """Parseia o OCR na borda e grava os `OcrText` ja tipados."""
    payload = OcrImportRequest.model_validate(await _import_payload(request, what="textos de OCR"))
    texts = OcrImporter.parse(payload.payload)
    imported = await ImportOcr(container).execute(media_id, texts, principal)
    return ImportResultOut(media_id=media_id, imported=imported, kind="ocr")


@router.post(
    "/media/{media_id}/detect",
    response_model=DetectionReportOut,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403, 404, 422),
    summary="Executa a deteccao",
    description=(
        "Roda o funil inteiro sobre a midia: janelas deslizantes, retrieval por "
        "keyword e embedding, rerank, fusao de score, juiz multimodal apenas na faixa "
        "de duvida, supressao de sobreposicao e refino de fronteira pelos cortes de "
        "cena. O corpo e opcional: `window_sizes`, `top_k` e `keep_rejected` "
        "sobrescrevem a configuracao apenas nesta execucao. **Reexecutar substitui as "
        "deteccoes anteriores da midia** — o funil e uma reanalise completa, e manter "
        "o resultado antigo ao lado do novo contaria a mesma veiculacao duas vezes. "
        "Midia sem transcricao responde `422` dizendo como importar uma."
    ),
)
async def detect_commercials(
    container: ContainerDep,
    principal: _Runner,
    media_id: _MediaId,
    payload: Annotated[
        DetectRequest | None,
        Body(description="Ajustes desta execucao; ausente usa a configuracao."),
    ] = None,
) -> DetectionReportOut:
    """Executa o funil e devolve o relatorio com as contagens e as evidencias."""
    options = payload or DetectRequest()
    report = await DetectCommercials(container).execute(
        media_id,
        principal,
        window_sizes=options.window_sizes,
        top_k=options.top_k,
        keep_rejected=options.keep_rejected,
    )
    return DetectionReportOut.from_result(report)


@router.get(
    "/media/{media_id}/detections",
    response_model=Page[DetectionOut],
    status_code=status.HTTP_200_OK,
    responses=_ITEM_ERRORS,
    summary="Deteccoes de uma midia",
    description=(
        "Pagina as deteccoes gravadas para o ativo, opcionalmente filtradas por "
        "`status` e por comercial. A rota resolve a midia antes de consultar: um "
        "identificador inexistente responde `404`, e nao uma pagina vazia que pareceria "
        "'nenhum comercial veiculado'."
    ),
)
async def list_media_detections(
    container: ContainerDep,
    principal: _Reader,
    pagination: PaginationDep,
    media_id: _MediaId,
    detection_status: Annotated[
        DetectionStatus | None,
        Query(alias="status", description="Restringe a accepted, needs_review ou rejected."),
    ] = None,
    commercial_id: Annotated[
        Id | None, Query(description="Restringe a um comercial (identificador interno).")
    ] = None,
) -> Page[DetectionOut]:
    """Devolve a pagina de deteccoes do ativo no envelope normativo da API."""
    asset = await GetMedia(container).execute(media_id, principal)
    filters = DetectionFilter(
        media_id=asset.id,
        commercial_id=commercial_id,
        status=detection_status,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    result = await ListDetections(container).execute(filters, principal)
    return Page[DetectionOut].from_result(result, DetectionOut.from_domain)


# ---------------------------------------------------------------------------
# Deteccoes
# ---------------------------------------------------------------------------
@router.get(
    "/detections",
    response_model=Page[DetectionOut],
    status_code=status.HTTP_200_OK,
    responses=_LIST_ERRORS,
    summary="Busca deteccoes",
    description=(
        "Consulta global das veiculacoes detectadas, filtrando por midia, por "
        "comercial e por `status`. `needs_review` isola a fila de revisao humana — os "
        "candidatos que ficaram entre os limiares e que o juiz multimodal nao "
        "promoveu, seja porque nao esta instalado, seja porque nao confirmou."
    ),
)
async def list_detections(
    container: ContainerDep,
    principal: _Reader,
    pagination: PaginationDep,
    media_id: Annotated[Id | None, Query(description="Restringe a um ativo de midia.")] = None,
    commercial_id: Annotated[
        Id | None, Query(description="Restringe a um comercial (identificador interno).")
    ] = None,
    detection_status: Annotated[
        DetectionStatus | None,
        Query(alias="status", description="Restringe a accepted, needs_review ou rejected."),
    ] = None,
) -> Page[DetectionOut]:
    """Devolve a pagina de deteccoes no envelope normativo da API."""
    filters = DetectionFilter(
        media_id=media_id,
        commercial_id=commercial_id,
        status=detection_status,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    result = await ListDetections(container).execute(filters, principal)
    return Page[DetectionOut].from_result(result, DetectionOut.from_domain)


@router.get(
    "/detections/{detection_id}",
    response_model=DetectionOut,
    status_code=status.HTTP_200_OK,
    responses=_ITEM_ERRORS,
    summary="Detalha uma deteccao",
    description=(
        "Devolve a deteccao com todas as evidencias que sustentam o score: "
        "similaridade lexica da locucao, semantica, OCR, veredito visual, aderencia a "
        "duracao esperada, se a ordem das frases-chave bateu e o trecho de transcricao "
        "que casou. E o que torna a deteccao auditavel — o revisor confere a decisao "
        "sem reabrir o video."
    ),
)
async def get_detection(
    container: ContainerDep, principal: _Reader, detection_id: _DetectionId
) -> DetectionOut:
    """Devolve a deteccao pedida com as suas evidencias."""
    detection = await GetDetection(container).execute(detection_id, principal)
    return DetectionOut.from_domain(detection)


@router.patch(
    "/detections/{detection_id}",
    response_model=DetectionOut,
    status_code=status.HTTP_200_OK,
    responses=_WRITE_ERRORS,
    summary="Revisa uma deteccao",
    description=(
        "Aplica o veredito humano sobre um candidato: confirmar (`accepted`), recusar "
        "(`rejected`) ou devolver para a fila (`needs_review`). As `notes` vao para o "
        "log de auditoria estruturado com autor, status anterior e status novo — "
        "`Detection` e um contrato normativo fechado e nao tem campo para a "
        "justificativa. O score e as evidencias nao sao recalculados: a revisao "
        "registra o julgamento humano, nao reescreve o que a maquina mediu."
    ),
)
async def review_detection(
    payload: DetectionReviewRequest,
    container: ContainerDep,
    principal: _Writer,
    detection_id: _DetectionId,
) -> DetectionOut:
    """Grava o veredito humano e devolve a deteccao atualizada."""
    detection = await ReviewDetection(container).execute(
        detection_id, payload.status, principal, notes=payload.notes
    )
    return DetectionOut.from_domain(detection)
