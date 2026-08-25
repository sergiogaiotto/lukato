# ADR-0004 — Retrieval e alinhamento antes do modelo multimodal (AdWatch)

**Status:** aceito · **Data:** 2026-08

## Contexto
A alternativa obvia seria enviar o video ao modelo multimodal e perguntar onde estao os
comerciais. Em milhares de horas isso e proibitivo, lento e nao explicavel.

## Decisao
O texto conhecido dos comerciais e tratado como **supervisao explicita**. O funil e
retrieval barato (janelas deslizantes + fingerprints) → TOP-K → rerank → fusao de score.
O modelo multimodal so julga a faixa de incerteza (0.60–0.90).

## Consequencias
**Positivas:** custo cai por ordens de grandeza; cada deteccao carrega evidencia
auditavel por sinal; o pipeline roda sem GPU quando ha transcricao.
**Negativas:** comerciais sem locucao e sem texto em tela dependem quase so do sinal
visual. Mitigacao registrada: quando existir um exemplar do comercial original,
acrescentar fingerprint de audio e de imagem ao indice multimodal.
