# SPEC-0010 — AdWatch: deteccao e localizacao temporal de comerciais

> **Status:** aceito · **Depende de:** SPEC-0000, SPEC-0007, SPEC-0011 · **Normativo.**

## 1. Problema

Dado (a) um catalogo conhecido de comerciais — **texto conhecido, com CRUD** — e (b)
horas de video, determinar **qual** comercial apareceu, **onde comecou**, **onde
terminou** e **quantas vezes**, com evidencia auditavel.

A decisao arquitetural central: isto **nao** e classificacao aberta de video. Como o
texto procurado ja e conhecido, o problema vira **matching temporal multimodal** —
majoritariamente *retrieval/alignment*, com o modelo multimodal atuando apenas como
juiz no fim do funil.

## 2. Pipeline

```text
                             VIDEO
                               │
              ┌────────────────┴────────────────┐
              ▼                                 ▼
            AUDIO                            FRAMES
              │                                 │
        ASR (WhisperX)                 Scene Detection + OCR
     palavras + timestamps            cortes/fades + texto na tela
              │                                 │
              └────────────────┬────────────────┘
                               ▼
                     LINHA DO TEMPO MULTIMODAL
                               │
                               ▼
                 JANELAS DESLIZANTES (15/30/60 s)
                               │
                               ▼
             RETRIEVAL sobre AD FINGERPRINTS  → TOP-K
                               │
                               ▼
                  RERANK (lexico + semantico + ordem)
                               │
                               ▼
                   FUSAO DE SCORE  S ∈ [0,1]
                     │            │           │
              S ≥ 0.90      0.60 ≤ S < 0.90   S < 0.60
                 │                │               │
              ACEITA        JUIZ MULTIMODAL     REJEITA
                 │           (Qwen VLM)
                 └────────┬───────┘
                          ▼
              REFINO DE FRONTEIRA (scene cuts)
                          ▼
                  DETECCAO PERSISTIDA
```

O modelo multimodal fica **no fim**, nunca no comeco: e caro e desnecessario para os
casos que o alinhamento textual ja resolve com alta confianca.

## 3. Estagios normativos

### 3.1 Ingestao
`RegisterMedia` cria um `MediaAsset` (`status="registered"`).
`IngestMedia` executa, quando o adaptador correspondente estiver disponivel:
1. `MediaProbePort.probe` → duracao/fps → atualiza o asset;
2. `MediaProbePort.extract_audio` → WAV mono 16 kHz;
3. `ASRPort.transcribe` → `list[TranscriptWord]` → `Transcript` (`source="whisperx"`);
4. `SceneDetectorPort.detect` → `list[SceneCut]`;
5. `OCRPort.extract` → `list[OcrText]`.
Cada etapa indisponivel e **registrada e pulada** (`status` reflete o alcancado);
nenhuma etapa indisponivel pode derrubar a ingestao.

`ImportTranscript` aceita JSON no formato WhisperX
(`[{"word","start","end"}]` ou `{"segments":[{"words":[...]}]}`) e cria o
`Transcript` com `source="import"`. **Este caminho torna o pipeline inteiro
executavel sem FFmpeg, sem GPU e sem rede** — e o caminho usado nos testes.

### 3.2 Fingerprints
`BuildFingerprint(commercial)` produz `AdFingerprint`:
* `normalized_text` = `normalize()` (minusculas, sem acento, pontuacao removida,
  espacos colapsados);
* `token_set` = tokens unicos ordenados;
* `keywords` = as informadas + termos de alto IDF do proprio texto (numeros, marcas,
  precos, unidades como `GB`, `R$`);
* `key_phrases` = as informadas ou n-gramas (n=4) mais distintivos;
* `embedding` = `EmbeddingPort.embed_one(normalized_text)`;
* `duration`, `expected_brand` copiados do comercial.
Reconstruido automaticamente sempre que `Commercial.text` muda.

### 3.3 Janelas deslizantes
`SlidingWindowBuilder(sizes=[15,30,60], stride=5)` gera janelas
`[t, t+size]` sobre a duracao da transcricao. Cada janela carrega o texto ASR do
intervalo e o texto OCR do mesmo intervalo. Janelas vazias sao descartadas.

### 3.4 Retrieval (TOP-K)
Nunca comparar o catalogo inteiro com LLM. Ordem:
1. **Filtro barato**: janela precisa conter ao menos 1 keyword do fingerprint
   (ou, se nao houver keywords, passa direto);
2. **Semantico**: cosseno entre embedding da janela e dos fingerprints →
   `top_k_retrieval` (padrao 10);
3. **Rerank**: score composto lexico+semantico+ordem → `top_k_rerank` (padrao 3).

### 3.5 Sinais de matching

| Sinal | Como e calculado | Peso |
| --- | --- | --- |
| `speech_match` (lexico) | `max` entre `rapidfuzz.fuzz.token_set_ratio`, `token_sort_ratio` e `partial_ratio` /100, sobre texto normalizado. Fallback puro-Python com `difflib.SequenceMatcher` quando `rapidfuzz` nao estiver instalado. | **0.40** |
| `semantic_match` | cosseno entre embeddings (janela × fingerprint), reescalado de `[-1,1]` para `[0,1]`. | **0.25** |
| `ocr_match` | melhor similaridade lexica entre o texto OCR da janela e `normalized_text`/`keywords`. `0.0` quando nao ha OCR. | **0.15** |
| `visual_match` | veredito do `VisionJudgePort`. Sem juiz disponivel, herda `speech_match` **e o fato e registrado em `evidence`** (nunca inventar 1.0). | **0.15** |
| `duration_match` | `1 - min(1, |dur_janela - duration_expected| / max(duration_expected, 1))`. | **0.05** |

**Ordem temporal** (`OrderMatcher`): as `key_phrases`/keywords precisam aparecer na
transcricao **na ordem esperada** dentro da janela. Implementado como subsequencia
comum mais longa (LCS) sobre a sequencia de tokens-ancora; devolve
`order_ratio ∈ [0,1]` e `order_ok = order_ratio >= 0.7`. Quando `order_ok` e falso,
o score final e multiplicado por `0.85` (penalidade), e o fato registrado na evidencia.

### 3.6 Fusao e decisao
```text
S = 0.40·speech + 0.25·semantic + 0.15·ocr + 0.15·visual + 0.05·duration
S ← S · (1.0 se order_ok senao 0.85)
```
Os pesos vem de `Settings.adwatch` e **devem somar 1.0**. Decisao:

| Faixa | `DetectionStatus` | Acao |
| --- | --- | --- |
| `S >= accept_threshold` (0.90) | `ACCEPTED` | aceita sem chamar o VLM |
| `review_threshold <= S < accept_threshold` | `NEEDS_REVIEW` | corta o trecho e chama `VisionJudgePort`; recalcula `S`; se passar de `accept_threshold`, vira `ACCEPTED` com `verified_by_vlm=True` |
| `S < review_threshold` (0.60) | `REJECTED` | descartado (persistido apenas se `keep_rejected=True`) |

### 3.7 Supressao de sobreposicao
Candidatos do mesmo comercial que se sobrepoem em mais de 50% do intervalo sao
fundidos, mantendo o de maior score (non-maximum suppression temporal).

### 3.8 Refino de fronteira
`BoundaryRefiner` ajusta `start`/`end` para os cortes de cena mais proximos, desde que
o deslocamento seja `<= 3.0 s` (configuravel). Marca `refined_by_scene=True`.
Sem cortes de cena, usa o primeiro/ultimo timestamp de palavra casada.

**Meta de negocio:** `recall > 98%`, `precision > 98%`, erro de inicio/fim `< 2 s`.

## 4. Prompt do juiz multimodal

O `VisionJudgePort` (implementado sobre o hub Qwen) recebe o trecho recortado, o texto
conhecido do comercial e o trecho de transcricao, e deve devolver **JSON estrito**:

```json
{
  "commercial_detected": true,
  "commercial_id": "COM_000234",
  "confidence": 0.96,
  "start": "01:21:33.4",
  "end": "01:22:03.1",
  "evidence": {
    "speech_match": 0.94, "visual_match": 0.97, "ocr_match": 0.88,
    "brand_detected": "Claro"
  }
}
```
Resposta que nao for JSON valido → `visual_match` nao e considerado e o candidato
permanece em `NEEDS_REVIEW` (nunca promovido por falha de parsing).

## 5. API `/api/v1/adwatch`

| Metodo | Rota | Descricao |
| --- | --- | --- |
| `POST` | `/commercials` | cria comercial (gera fingerprint) |
| `GET` | `/commercials` | lista com `search`, `brand`, `campaign`, `is_active`, paginacao |
| `GET` | `/commercials/{id}` | detalhe + fingerprint |
| `PUT` | `/commercials/{id}` | atualiza (regera fingerprint se o texto mudou) |
| `DELETE` | `/commercials/{id}` | remove |
| `POST` | `/commercials/bulk` | importacao em lote (JSON array) |
| `POST` | `/media` | registra midia |
| `GET` | `/media` · `/media/{id}` | lista / detalhe (inclui capacidades disponiveis) |
| `POST` | `/media/{id}/ingest` | executa a ingestao possivel |
| `GET` | `/media/{id}/transcript` | le a transcricao palavra a palavra; `?q=` localiza uma frase exata na linha do tempo |
| `POST` | `/media/{id}/transcript` | importa transcricao JSON |
| `POST` | `/media/{id}/scenes` | importa cortes de cena JSON |
| `POST` | `/media/{id}/ocr` | importa OCR JSON |
| `POST` | `/media/{id}/detect` | executa a deteccao; body: `{"window_sizes":[...], "top_k":10, "keep_rejected":false}` |
| `GET` | `/media/{id}/detections` | deteccoes da midia |
| `GET` | `/detections` | busca global com filtros |
| `GET` | `/detections/{id}` | detalhe com evidencias |
| `PATCH` | `/detections/{id}` | revisao humana (`status`, `notes`) |
| `GET` | `/capabilities` | quais adaptadores de midia estao disponiveis |

## 6. Criterios de aceite

1. CRUD completo de comerciais pela API e pela UI.
2. Com uma transcricao importada e um catalogo de comerciais, `POST /media/{id}/detect`
   devolve deteccoes com `start`/`end`/`confidence`/`evidence` **sem rede e sem FFmpeg**.
3. Os pesos e limiares vem de `Settings` e sao verificados por teste.
4. Um comercial cujo texto aparece com variacao lexical
   (`"aproveite sua internet"` vs `"aproveite muito mais da sua internet"`)
   ainda e detectado — cobertura por teste.
5. Palavras do comercial presentes fora de ordem, em regioes distintas, **nao**
   produzem deteccao aceita — cobertura por teste.
6. `GET /adwatch/capabilities` reporta corretamente o que esta instalado.
