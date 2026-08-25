# SPEC-0009 — Console web (template engine)

> **Status:** aceito · **Depende de:** SPEC-0000 · **Normativo.**
> Referencia visual: console operacional de tres colunas (menu recolhivel a esquerda,
> operacao do modulo ao centro, detalhes por contexto a direita).

## 1. Principios

1. **Server-side rendering puro** com Jinja2. Sem framework SPA, sem build step.
2. **Zero CDN**: todo CSS/JS/fonte e servido de `interfaces/ui/static/`. A aplicacao
   precisa renderizar identica em rede fechada.
3. Progressive enhancement: toda tela funciona com formularios HTML; o JavaScript
   apenas melhora (busca incremental, painel de contexto, toasts, atalhos).
4. Acessibilidade: landmarks (`header`/`nav`/`main`/`aside`/`footer`), `aria-current`,
   foco visivel, contraste AA, navegacao por teclado.
5. O console consome a **propria API v1** via `fetch` (mesma origem) — nunca acessa
   repositorios diretamente.

## 2. Arquivos

```text
src/lukato/interfaces/ui/
├── __init__.py
├── router.py                     # APIRouter com todas as rotas de pagina
├── context.py                    # helpers de contexto (nav, principal, flags, filtros)
├── filters.py                    # filtros Jinja: money, tokens, duration, timeago, badge...
├── templates/
│   ├── base.html                 # esqueleto: topbar + sidebar + main + aside + statusbar
│   ├── partials/
│   │   ├── topbar.html  sidebar.html  statusbar.html  context_panel.html
│   │   ├── hero.html    card.html     stat.html      table.html
│   │   ├── empty.html   toast.html    pill.html      badge.html
│   │   └── pagination.html
│   ├── pages/
│   │   ├── cockpit.html
│   │   ├── modules_list.html      modules_detail.html
│   │   ├── prompts.html           guardrails.html
│   │   ├── runs.html              run_detail.html
│   │   ├── knowledge.html         finops.html
│   │   ├── observability.html     registry.html
│   │   ├── adwatch.html           adwatch_commercials.html   adwatch_detections.html
│   │   ├── identity.html          settings.html
│   │   └── error.html
│   └── context/                  # conteudo do painel direito, por modulo
│       ├── module.html  prompt.html  guardrail.html  run.html
│       ├── document.html  commercial.html  detection.html  default.html
└── static/
    ├── css/tokens.css  base.css  layout.css  components.css  pages.css
    ├── js/app.js  sidebar.js  context.js  palette.js  charts.js
    └── img/logo.svg  favicon.svg
```

## 3. Layout obrigatorio (`base.html`)

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ TOPBAR  [L] lukato / <trilha>      [busca global ⌘K]      API · SAIR · user│
├──────────┬───────────────────────────────────────────┬─────────────────────┤
│          │                                           │                     │
│ SIDEBAR  │  MAIN (operacao do modulo)                │ ASIDE               │
│ 236px    │  hero + cards + formularios + tabelas     │ PAINEL DE CONTEXTO  │
│ recolhe  │                                           │ 320px               │
│ p/ 60px  │                                           │ detalhes do item    │
│          │                                           │ selecionado         │
├──────────┴───────────────────────────────────────────┴─────────────────────┤
│ STATUSBAR  guardrails · langfuse · otel   v1.0.0    custos por modulo · total│
└────────────────────────────────────────────────────────────────────────────┘
```

Grade CSS:
```css
.lk-app { display: grid; grid-template-columns: var(--lk-sidebar-w) 1fr var(--lk-aside-w);
          grid-template-rows: var(--lk-topbar-h) 1fr var(--lk-statusbar-h);
          grid-template-areas: "topbar topbar topbar" "sidebar main aside" "status status status"; }
```
`--lk-sidebar-w` alterna entre `236px` e `60px` via atributo `data-sidebar="expanded|collapsed"`
no `<html>`. O estado persiste em `localStorage["lukato.sidebar"]` e e restaurado por um
script inline **antes** do primeiro paint (evita flash).

Blocos Jinja expostos por `base.html`:
`title`, `breadcrumb`, `hero`, `toolbar`, `content`, `context_title`, `context`, `scripts`, `head_extra`.

Responsivo:
* `<= 1280px`: `aside` vira gaveta (`data-aside="open|closed"`), aberta por botao.
* `<= 900px`: sidebar vira overlay; grid passa a uma coluna.
* Tabelas largas ficam dentro de `.lk-scroll-x { overflow-x: auto; }`.

## 4. Navegacao (sidebar)

Definida em `context.py` como `NAV_SECTIONS: list[NavSection]`; modulos registrados
podem **acrescentar** itens via `BaseModule.ui().nav`.

| Secao | Itens (label · rota · icone) |
| --- | --- |
| *(sem titulo)* | Cockpit · `/` · `home` |
| `FUNCIONALIDADE` | Módulos · `/modules` · `blocks` · Execuções · `/runs` · `activity` · Conhecimento · `/knowledge` · `book` · AdWatch · `/adwatch` · `film` |
| `CONFIGURAÇÕES` | Prompts · `/prompts` · `message` · Guardrails · `/guardrails` · `shield` · Registry · `/registry` · `plug` |
| `MONITORAMENTO` | FinOps · `/finops` · `coins` · Observabilidade · `/observability` · `pulse` |
| `ADMINISTRATIVO` | Identidade · `/identity` · `users` · Configurações · `/settings` · `sliders` |

Item ativo: fundo `--lk-accent-soft`, barra vertical de 3px em `--lk-accent`, texto
`--lk-accent-strong`, `aria-current="page"`. Recolhido, mostra apenas o icone com
`title`/`aria-label`.

Icones: **SVG inline** em `partials/sidebar.html` via macro `{% macro icon(name) %}`
(sprite `<symbol>` em `static/img/icons.svg` tambem e aceito). Nenhuma fonte de icones externa.

## 5. Tokens visuais (`static/css/tokens.css`)

```css
:root {
  --lk-accent:        #c8102e;   --lk-accent-strong: #a30f1c;
  --lk-accent-deep:   #7a0c16;   --lk-accent-soft:   #fdeef0;
  --lk-ai:            #1a56db;   --lk-ai-soft:       #eaf0fd;
  --lk-ok:            #17803d;   --lk-warn:          #b45309;   --lk-danger: #b91c1c;
  --lk-bg:            #f6f7f9;   --lk-panel:         #ffffff;
  --lk-text:          #0f1115;   --lk-muted:         #5b6270;   --lk-faint: #8b909c;
  --lk-border:        #e4e6eb;   --lk-border-strong: #cfd3da;
  --lk-radius:        10px;      --lk-radius-sm:     6px;
  --lk-shadow:        0 1px 2px rgba(15,17,21,.06), 0 1px 8px rgba(15,17,21,.04);
  --lk-topbar-h:      52px;      --lk-statusbar-h:   34px;
  --lk-sidebar-w:     236px;     --lk-aside-w:       320px;
  --lk-font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --lk-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
```
Tema escuro em `@media (prefers-color-scheme: dark)` **e** em `:root[data-theme="dark"]`,
com `:root[data-theme="light"]` vencendo o media query. Nenhuma cor pode ter sua
**unica** definicao dentro de um bloco de tema.

## 6. Componentes obrigatorios (`components.css` + `partials/`)

| Componente | Classe | Notas |
| --- | --- | --- |
| Hero do modulo | `.lk-hero` | gradiente `--lk-accent-deep` → `--lk-accent`, texto branco, marcador quadrado antes do titulo, subtitulo em 13px/0.9 opacidade, area de acoes a direita (botao outline branco) e uma linha de metricas textuais. |
| Cartao | `.lk-card` | fundo `--lk-panel`, borda 1px, raio `--lk-radius`, sombra `--lk-shadow`. Titulo em `.lk-card__label` (11px, uppercase, letter-spacing .06em, `--lk-muted`) precedido de um ponto colorido `.lk-dot`. |
| Cartao IA | `.lk-card--ai` | borda superior 2px `--lk-ai` e ponto azul; usado nos paineis gerados por IA. |
| Pill/toggle | `.lk-pill` | botao com icone + texto; `.is-active` ganha borda e texto `--lk-accent`. |
| Botao | `.lk-btn`, `.lk-btn--primary` (vermelho solido), `.lk-btn--ghost`, `.lk-btn--outline` | altura 34px, raio 8px. |
| Campo | `.lk-field`, `.lk-input`, `.lk-select`, `.lk-textarea` | rotulo 11px uppercase, ajuda 11px `--lk-faint`; inputs de identificador usam `--lk-mono`. |
| Tile de metrica | `.lk-stat` | numero 26px semibold, legenda 10px uppercase, badge `i` opcional com `title`; `.is-primary` destaca com borda `--lk-ai`. |
| Barra de progresso | `.lk-progress` | trilho 4px, preenchimento `--lk-accent`, rotulo a direita. |
| Badge | `.lk-badge` + `--ok/--warn/--danger/--info/--neutral` | pill 11px com ponto. |
| Linha de especialistas/atalhos | `.lk-picker` | grade de cartoes clicaveis com chevron; `.is-suggested` recebe badge flutuante azul. |
| Tabela | `.lk-table` | cabecalho 11px uppercase, zebra sutil, linha clicavel abre o painel de contexto. |
| Vazio | `.lk-empty` | icone + frase + acao sugerida. |
| Toast | `.lk-toast` | canto inferior direito, auto-dismiss 4s, papel `status`. |
| Barra de status | `.lk-statusbar` | esquerda: indicadores de saude com ponto colorido (`guardrails`, `langfuse`, `otel`, `db`) + `v1.0.0`; direita: custo por modulo (ponto colorido + `US$ 0,00000`), barra proporcional, **Custo total** e contagem de execucoes. |

## 7. Painel de contexto (aside)

Requisito funcional: *"a direita deve oferecer detalhes por contexto de cada item de
cada modulo"*.

* Sempre renderizado; sem selecao, mostra `context/default.html` (resumo do modulo atual
  + atalhos + dicas).
* Toda linha de tabela/cartao selecionavel carrega
  `GET /ui/context/{entity}/{id}` (fragmento HTML) e substitui `#lk-context-body`
  via `fetch`, atualizando `history.replaceState` com `?sel=<id>`.
* Sem JavaScript, o mesmo link e um `<a href="?sel=<id>">` que renderiza o painel no
  servidor. **A funcionalidade nao pode depender de JS.**
* Entidades suportadas: `module`, `prompt`, `guardrail`, `run`, `document`,
  `commercial`, `detection`, `apikey`, `user`.
* Estrutura do painel: titulo + badge de status, lista de pares chave/valor
  (`.lk-kv`), bloco de acoes contextuais, bloco de JSON bruto colapsavel
  (`<details><summary>JSON</summary><pre>`).

## 8. Rotas de pagina (`router.py`)

| Rota | Template | Conteudo central |
| --- | --- | --- |
| `GET /` | `cockpit.html` | tiles (modulos ativos, execucoes 24h, custo 24h, bloqueios de guardrail), saude dos provedores, ultimas execucoes, custo por modulo |
| `GET /modules` | `modules_list.html` | filtros (kind/status/busca), tabela de modulos, botao "novo modulo" |
| `GET /modules/{slug}` | `modules_detail.html` | **operacao do modulo**: form de invocacao (input, variaveis JSON), binding guardrail-in/prompt/guardrail-out editavel, resultado + findings + custo |
| `GET /prompts` | `prompts.html` | CRUD + preview de renderizacao com variaveis |
| `GET /guardrails` | `guardrails.html` | CRUD de politicas, editor de regras, **testador** (texto → veredito) |
| `GET /runs` | `runs.html` | filtros, tabela paginada |
| `GET /runs/{id}` | `run_detail.html` | linha do tempo de steps, tokens, custo, trace |
| `GET /knowledge` | `knowledge.html` | colecoes, ingestao de documento, busca semantica |
| `GET /finops` | `finops.html` | resumo, custo por modulo/modelo, orcamentos, serie temporal (SVG inline) |
| `GET /observability` | `observability.html` | estado do Langfuse/OTel, ultimos traces, scores |
| `GET /registry` | `registry.html` | building blocks descobertos, capacidades, schema de config |
| `GET /adwatch` | `adwatch.html` | pipeline: registrar midia, importar transcricao, executar deteccao |
| `GET /adwatch/commercials` | `adwatch_commercials.html` | **CRUD do catalogo de comerciais** |
| `GET /adwatch/detections` | `adwatch_detections.html` | deteccoes com evidencias e linha do tempo |
| `GET /identity` | `identity.html` | usuarios, papeis, API keys |
| `GET /settings` | `settings.html` | provedores (LLM/embedding), guardrails globais, AdWatch, saude — **somente leitura de segredos** (mascarados) |
| `GET /ui/context/{entity}/{id}` | `context/*.html` | fragmento do painel direito |

Todas as paginas recebem, via `context.py`, um dicionario base com:
`nav_sections`, `active_route`, `breadcrumb`, `principal`, `settings_public`,
`health`, `cost_summary`, `version`, `request_id`, `selected_id`.

## 9. JavaScript (`static/js/`)

* `app.js` — bootstrap, toasts, `fetchJSON` com tratamento de erro padrao
  (`{"error":{"code","message"}}`), formatacao de numeros pt-BR, confirmacoes.
* `sidebar.js` — recolher/expandir + persistencia + atalho `[`.
* `context.js` — carregar fragmento do painel direito, selecao de linha, `?sel=`.
* `palette.js` — paleta de comandos (`⌘K` / `Ctrl+K`): navegacao por rotas e modulos,
  filtro incremental, teclado.
* `charts.js` — sparkline/barra em **SVG puro** a partir de `data-series` (sem libs).

Tudo em ES2020, sem bundler, sem dependencias externas, carregado com `defer`.

## 10. Seguranca da UI

* Autoescape do Jinja **ligado** (padrao para `.html`). `|safe` somente em HTML
  gerado pelo proprio servidor.
* Formularios de mutacao usam `POST` com token CSRF (`itsdangerous`) quando
  `security.auth_enabled` estiver ligado.
* Segredos nunca sao renderizados: `settings_public` mascara `SecretStr` como
  `sk-…últimos4` ou `(nao configurado)`.
* `Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'`
  aplicada por middleware (inline style e usado apenas em barras de progresso).

## 11. Criterios de aceite

1. `GET /` responde 200 e o HTML contem `topbar`, `sidebar`, `main`, `aside`, `statusbar`.
2. Recolher a sidebar e recarregar mantem o estado (localStorage).
3. Cada rota da secao 8 responde 200 com o `<title>` correto.
4. Com JavaScript desabilitado, `?sel=<id>` ainda renderiza o painel de contexto.
5. Nenhuma referencia a host externo em templates ou CSS
   (`grep -rE "https?://(?!localhost)" src/lukato/interfaces/ui/` nao encontra
   nada alem de links de documentacao em comentarios).
6. Tema claro e escuro legiveis; contraste AA nos textos principais.
