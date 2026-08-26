"""Navegacao fim a fim do console: TODAS as 30 operacoes de escrita, pela tela.

POR QUE ISTO EXISTE, E NAO SO A BATERIA DE TESTES
=================================================

A bateria confere que os campos de cada formulario batem com o schema do
endpoint, e isso pegou defeito de verdade. Mas ela nao CLICA. Cinco defeitos so
apareceram quando alguem clicou:

* toda acao de linha estava morta — o ouvinte do painel de contexto engolia o
  clique de qualquer botao dentro de uma `<tr>`, entao aceitar deteccao, remover
  comercial, rotacionar chave e afins nao gravavam nada, sem erro nenhum;
* preview de prompt e teste de politica eram redirecionados para longe do proprio
  resultado;
* a tela de Conhecimento prometia que a ingestao continuava funcionando num
  estado em que ela falhava;
* salvar um item devolvia uma lista paginada onde o item recem-criado nao
  aparecia.

COMO RODAR

    python scripts/navegacao_fim_a_fim.py           # numero da rodada automatico
    python scripts/navegacao_fim_a_fim.py 42        # numero escolhido a mao

Exige a aplicacao no ar em http://127.0.0.1:8000 e o Chromium do Playwright.
Cada rodada cria itens com sufixo proprio, entao rodar de novo nao colide e nao
apaga nada: e assim que se mede estabilidade aqui.

O QUE ELE ASSEGURA, E O QUE NAO
===============================

Cada operacao confere o ESTADO depois do clique, lendo a API. A versao anterior
assertava so "a URL nao caiu no /api/" e dava verde em cinco operacoes que nao
tinham gravado nada — um clique que nunca submete tambem satisfaz isso. Foi
trocar essa assercao que revelou o defeito do painel de contexto.

Ele nao cobre: autenticacao ligada, varios inquilinos, e o caminho com FFmpeg e
provedor de LLM alcancaveis. Nesses estados a aplicacao segue outro ramo.
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

from playwright.sync_api import Page, sync_playwright

BASE = "http://127.0.0.1:8000"


def _proxima_rodada() -> str:
    """Numero da rodada, escolhido sozinho quando nao vem por argumento.

    Criar duas vezes o mesmo slug e um 409 legitimo — o console esta certo em
    recusar. Entao cada rodada trabalha no proprio espaco de nomes, e "rodar de
    novo" passa a significar "a aplicacao aguenta a segunda passagem convivendo
    com o que a primeira deixou", que e a estabilidade que interessa aqui.
    """
    contador = pathlib.Path("/tmp/e2e/rodada.txt")
    if len(sys.argv) > 1:
        return sys.argv[1]
    n = int(contador.read_text()) + 1 if contador.exists() else 1
    contador.write_text(str(n))
    return str(n)


RODADA = _proxima_rodada()
SAIDA = pathlib.Path(f"/tmp/e2e/prints-r{RODADA}")
VIDEO = pathlib.Path(f"/tmp/e2e/video-r{RODADA}")
SAIDA.mkdir(parents=True, exist_ok=True)
VIDEO.mkdir(parents=True, exist_ok=True)

SUF = f"-r{RODADA}"
resultados: list[tuple[str, str, str]] = []  # (area, operacao, "ok" | motivo)
aceita_id: list[str] = []  # deteccoes aceitas nesta rodada, para nao rejeita-las
_pagina_atual: list = []  # preenchido quando a pagina abre; usado no print de falha
n_passo = 0


def api(caminho: str) -> dict:
    """Le a API para CONFERIR o efeito. A acao continua sendo feita pela tela.

    Existe porque a rodada anterior deu verde em cinco operacoes que nao tinham
    gravado nada: a assercao era "a URL nao caiu no /api/", e um clique que nunca
    submete tambem satisfaz isso. Conferir o estado e o que separa "o botao nao
    deu erro" de "o botao fez o que diz".
    """
    with urllib.request.urlopen(BASE + caminho, timeout=20) as r:
        return json.load(r)


def print_(pagina: Page, nome: str) -> None:
    global n_passo
    n_passo += 1
    alvo = SAIDA / f"{n_passo:02d}-{nome.lower().replace(' ', '-').replace('/', '-')}.png"
    pagina.screenshot(path=str(alvo))


def op(area: str, nome: str):
    """Decorador-executor: roda a operacao, registra o desfecho, nao interrompe."""

    def executar(fn):
        try:
            fn()
            resultados.append((area, nome, "ok"))
            print(f"  ok    {area:<12} {nome}", flush=True)
        except Exception as erro:
            motivo = f"{type(erro).__name__}: {erro}".split("\n")[0][:200]
            resultados.append((area, nome, motivo))
            print(f"  FALHA {area:<12} {nome}\n          {motivo}", flush=True)
            # Print no momento da falha: sem ele, "Locator.fill: Timeout" nao diz
            # se o campo nao existe, se algo o cobre, ou se a pagina e outra.
            try:
                _pagina_atual[0].screenshot(
                    path=str(SAIDA / f"FALHA-{area}-{nome.replace(' ', '-')[:40]}.png")
                )
                print(f"          url no momento da falha: {_pagina_atual[0].url}", flush=True)
            except Exception:
                pass
        return fn

    return executar


def salvou(pagina: Page, formulario, botao: str | None = None) -> None:
    """Submete e exige o 303 do POST-Redirect-GET; erro vira excecao com o texto."""
    alvo = (
        formulario.locator(f'button:has-text("{botao}")').first
        if botao
        else formulario.locator('button[type="submit"]').first
    )
    with pagina.expect_navigation(wait_until="networkidle"):
        alvo.click()
    if "/api/" in pagina.url:
        titulo = pagina.locator("h1").first
        detalhe = titulo.inner_text() if titulo.count() else ""
        msg = pagina.locator(".lk-error__message").first
        if msg.count():
            detalhe += " — " + msg.inner_text().strip()
        raise AssertionError(f"nao gravou ({pagina.url}): {detalhe}")


def form_com(pagina: Page, seletor: str):
    """Primeiro formulario que contem o seletor dado."""
    return pagina.locator("form").filter(has=pagina.locator(seletor)).first


def linha(pagina: Page, texto: str):
    """Abre o item da linha que contem o texto, pelo link da primeira celula.

    Clicar no `<tr>` NAO navega: o `context.js` intercepta e so carrega o painel
    da direita. Quem leva ao `?sel=` e o link da celula, e e por ele que um
    usuario chega ao editor.
    """
    # Filtra ANTES de procurar a linha. Com 27 prompts cadastrados a lista pagina
    # em 25 e ordena por slug: o item recem-criado cai na segunda pagina. Um
    # usuario nessa situacao usa a busca, e e o que o roteiro faz.
    caminho = pagina.url.split("?")[0].replace(BASE, "")
    pagina.goto(f"{BASE}{caminho}?q={texto}", wait_until="networkidle")
    alvo = pagina.locator("tbody tr").filter(has_text=texto).first
    assert alvo.count(), f"a busca por '{texto}' nao trouxe linha nenhuma"
    destino = alvo.locator('a[href*="sel="]').first.get_attribute("href")
    assert destino, f"a linha '{texto}' nao tem link de selecao"
    pagina.goto(BASE + destino, wait_until="networkidle")


def midia_da_rodada(pagina: Page):
    """A linha da midia que ESTA rodada registrou, nao a primeira da lista.

    Mirar a primeira linha fazia cada rodada re-detectar a midia da rodada 1. Uma
    re-deteccao substitui as deteccoes DAQUELA midia (o proprio console avisa
    disso), entao o desfecho que a rodada anterior marcou como aceito era apagado
    pela seguinte, e o banco terminava sem nenhuma deteccao aceita. Nao era
    defeito da aplicacao: era o roteiro batendo sempre no mesmo ativo.
    """
    linha = pagina.locator("tr").filter(has_text=f"rodada {RODADA}").first
    assert linha.count(), f"a midia da rodada {RODADA} nao esta na lista"
    return linha


def transcricao(inicio: float, fim: float, falas: str) -> str:
    palavras = falas.split()
    passo = (fim - inicio) / len(palavras)
    itens = [
        {
            "word": p,
            "start": round(inicio + i * passo, 2),
            "end": round(inicio + (i + 1) * passo, 2),
        }
        for i, p in enumerate(palavras)
    ]
    return json.dumps(
        [
            {"word": "jornal", "start": 5.0, "end": 5.4},
            *itens,
            {"word": "voltamos", "start": fim + 12, "end": fim + 12.5},
        ],
        ensure_ascii=False,
    )


FALAS = (
    "chegou a vivo fibra de trezentos mega para a sua casa "
    "instale hoje sem taxa de adesao e leve wifi seis incluso "
    "vivo fibra trezentos mega ligue agora"
)

with sync_playwright() as pw:
    navegador = pw.chromium.launch(
        executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome", headless=True
    )
    contexto = navegador.new_context(
        viewport={"width": 1600, "height": 950},
        record_video_dir=str(VIDEO),
        record_video_size={"width": 1600, "height": 950},
        locale="pt-BR",
    )
    contexto.on("dialog", lambda d: d.accept())
    pagina = contexto.new_page()
    pagina.set_default_timeout(25_000)
    _pagina_atual.append(pagina)

    print(f"\n=== RODADA {RODADA} ===\n", flush=True)

    # ---------------------------------------------------------------- PROMPTS
    pagina.goto(f"{BASE}/prompts", wait_until="networkidle")
    print_(pagina, "prompts")

    @op("prompts", "criar prompt")
    def _():
        f = pagina.locator("#editor form").first
        f.locator('input[name="slug"]').fill(f"revisor-adwatch{SUF}")
        f.locator('input[name="name"]').fill(f"Revisor AdWatch {RODADA}")
        f.locator('select[name="role"]').select_option("system")
        f.locator('textarea[name="template"]').fill(
            "Voce revisa deteccoes da marca {{ marca }} na campanha {{ campanha }}. "
            "Responda apenas 'aceito' ou 'rejeitado' e uma frase de motivo."
        )
        f.locator('input[name="labels[]"]').fill("adwatch, revisao, producao")
        f.locator('input[name="description"]').fill("Revisao humana assistida de deteccao.")
        salvou(pagina, f)

    @op("prompts", "editar prompt (gera versao nova)")
    def _():
        pagina.goto(f"{BASE}/prompts", wait_until="networkidle")
        linha(pagina, f"revisor-adwatch{SUF}")
        f = pagina.locator("#editor form").first
        f.locator('input[name="name"]').fill(f"Revisor AdWatch {RODADA} (revisado)")
        f.locator('textarea[name="template"]').fill(
            "Voce revisa deteccoes da marca {{ marca }} na campanha {{ campanha }}. "
            "Considere a ordem das frases-chave. Responda 'aceito' ou 'rejeitado'."
        )
        salvou(pagina, f)
        print_(pagina, "prompt-editado")

    @op("prompts", "pre-visualizar prompt")
    def _():
        pagina.goto(f"{BASE}/prompts", wait_until="networkidle")
        linha(pagina, f"revisor-adwatch{SUF}")
        f = form_com(pagina, 'input[name^="variables["]')
        assert "/prompts/preview" in (f.get_attribute("action") or ""), (
            "o preview ainda posta em /api/, onde o 303 descarta o resultado"
        )
        for campo in f.locator('input[name^="variables["]').all():
            campo.fill("VIVO" if "marca" in (campo.get_attribute("name") or "") else "fibra-300")
        with pagina.expect_navigation(wait_until="networkidle"):
            f.locator('button[type="submit"]').first.click()
        corpo = pagina.locator("body").inner_text()
        assert "VIVO" in corpo, "o texto renderizado nao trouxe a variavel substituida"
        print_(pagina, "prompt-preview")

    # ------------------------------------------------------------- GUARDRAILS
    pagina.goto(f"{BASE}/guardrails", wait_until="networkidle")

    @op("guardrails", "criar politica com regra")
    def _():
        f = pagina.locator("#editor form").first
        f.locator('input[name="slug"]').fill(f"saida-adwatch{SUF}")
        f.locator('input[name="name"]').fill(f"Saida AdWatch {RODADA}")
        f.locator('select[name="stage"]').select_option("output")
        f.locator('input[name="description"]').fill("Redige dado pessoal no laudo de deteccao.")
        f.locator('input[name="rules[0].id"]').fill("pii")
        f.locator('select[name="rules[0].kind"]').select_option("pii_redact")
        f.locator('select[name="rules[0].action"]').select_option("redact")
        f.locator('input[name="rules[0].message"]').fill("Dado pessoal removido.")
        f.locator('textarea[name="rules[0].config{}"]').fill('{"types": ["cpf", "phone"]}')
        salvou(pagina, f)
        print_(pagina, "guardrail-criado")

    @op("guardrails", "editar politica (acrescenta 2a regra)")
    def _():
        pagina.goto(f"{BASE}/guardrails", wait_until="networkidle")
        linha(pagina, f"saida-adwatch{SUF}")
        f = pagina.locator("#editor form").first
        f.locator('input[name="rules[1].id"]').fill("tamanho")
        f.locator('select[name="rules[1].kind"]').select_option("max_length")
        f.locator('select[name="rules[1].action"]').select_option("transform")
        f.locator('textarea[name="rules[1].config{}"]').fill('{"max_chars": 4000}')
        salvou(pagina, f)
        texto = pagina.locator("tbody").inner_text()
        assert f"saida-adwatch{SUF}" in texto
        print_(pagina, "guardrail-editado")

    @op("guardrails", "testar politica")
    def _():
        pagina.goto(f"{BASE}/guardrails", wait_until="networkidle")
        f = form_com(pagina, 'textarea[name="content"]')
        f.locator('select[name="policy"]').select_option(f"saida-adwatch{SUF}")
        f.locator('textarea[name="content"]').fill(
            "O cliente 529.982.247-25 pediu retorno no telefone 11 98888-7777."
        )
        with pagina.expect_navigation(wait_until="networkidle"):
            f.locator('button[type="submit"]').first.click()
        assert "/api/" not in pagina.url, f"o teste de politica caiu em {pagina.url}"
        print_(pagina, "guardrail-testado")

    # ---------------------------------------------------------------- MODULOS
    @op("modulos", "criar modulo")
    def _():
        pagina.goto(f"{BASE}/modules", wait_until="networkidle")
        f = pagina.locator("#novo-modulo form").first
        f.locator('input[name="slug"]').fill(f"revisor{SUF}")
        f.locator('input[name="name"]').fill(f"Revisor de deteccao {RODADA}")
        f.locator('select[name="kind"]').select_option("agent")
        f.locator('[name="description"]').fill("Agente que revisa deteccao do AdWatch.")
        salvou(pagina, f)
        print_(pagina, "modulo-criado")

    @op("modulos", "salvar trinca (binding)")
    def _():
        pagina.goto(f"{BASE}/modules/assistente", wait_until="networkidle")
        f = form_com(pagina, 'input[name="binding.model"]')
        f.locator('input[name="binding.temperature"]').fill("0.25")
        f.locator('input[name="binding.max_tokens"]').fill("1500")
        f.locator('input[name="binding.tools[]"]').fill("now, calculator")
        salvou(pagina, f, botao="Salvar trinca")
        print_(pagina, "trinca-salva")

    @op("modulos", "pausar modulo")
    def _():
        pagina.goto(f"{BASE}/modules/assistente", wait_until="networkidle")
        f = form_com(pagina, 'button[value="paused"]')
        with pagina.expect_navigation(wait_until="networkidle"):
            f.locator('button[value="paused"]').first.click()
        assert "/api/" not in pagina.url, f"pausar caiu em {pagina.url}"

    @op("modulos", "reativar modulo")
    def _():
        pagina.goto(f"{BASE}/modules/assistente", wait_until="networkidle")
        f = form_com(pagina, 'button[value="active"]')
        with pagina.expect_navigation(wait_until="networkidle"):
            f.locator('button[value="active"]').first.click()
        assert "/api/" not in pagina.url, f"reativar caiu em {pagina.url}"

    @op("modulos", "invocar modulo")
    def _():
        pagina.goto(f"{BASE}/modules/assistente", wait_until="networkidle")
        f = form_com(pagina, 'textarea[name="input"]')
        f.locator('textarea[name="input"]').fill(
            f"Rodada {RODADA}: resuma a deteccao do comercial VIVO_FIBRA_300 no bloco da noite."
        )
        antes = api("/api/v1/runs?limit=1")["total"]
        salvou(pagina, f, botao="Executar")
        assert api("/api/v1/runs?limit=1")["total"] > antes, (
            "o clique em Executar nao gravou execucao nenhuma"
        )
        print_(pagina, "modulo-invocado")

    # ----------------------------------------------------------- CONHECIMENTO
    @op("conhecimento", "ingerir documento")
    def _():
        pagina.goto(f"{BASE}/knowledge", wait_until="networkidle")
        f = form_com(pagina, 'textarea[name="content"]')
        f.locator('input[name="title"]').fill(f"Politica de veiculacao {RODADA}")
        f.locator('input[name="source"]').fill(f"manual-adwatch{SUF}")
        f.locator('textarea[name="content"]').fill(
            "Uma veiculacao e aceita quando a confianca fica em 0,90 ou acima. "
            "Entre 0,60 e 0,90 a deteccao vai para revisao humana. "
            "Abaixo de 0,60 a deteccao e rejeitada automaticamente."
        )
        salvou(pagina, f, botao="Ingerir")
        docs = api("/api/v1/knowledge/documents")
        assert docs["total"] >= 1, "a ingestao redirecionou mas nao gravou documento"
        print_(pagina, "documento-ingerido")

    @op("conhecimento", "busca semantica")
    def _():
        pagina.goto(f"{BASE}/knowledge", wait_until="networkidle")
        f = form_com(pagina, 'input[id="search-query"], input[name="q"][id^="search"]')
        f.locator('input[name="q"]').first.fill("quando a deteccao vai para revisao")
        with pagina.expect_navigation(wait_until="networkidle"):
            f.locator('button[type="submit"]').first.click()
        print_(pagina, "busca-semantica")

    # --------------------------------------------------------------- ADWATCH
    @op("adwatch", "criar comercial")
    def _():
        pagina.goto(f"{BASE}/adwatch/commercials", wait_until="networkidle")
        f = form_com(pagina, 'input[name="commercial_id"]')
        f.locator('input[name="commercial_id"]').fill(f"VIVO_FIBRA_300{SUF.upper()}")
        f.locator('input[name="brand"]').fill("VIVO")
        f.locator('input[name="campaign"]').fill(f"fibra-300-rodada-{RODADA}")
        f.locator('input[name="duration_expected"]').fill("15")
        f.locator('textarea[name="text"]').fill(FALAS)
        f.locator('input[name="keywords[]"]').fill("vivo, fibra, trezentos mega, wifi seis")
        f.locator('input[name="key_phrases[]"]').fill(
            "vivo fibra trezentos mega, sem taxa de adesao"
        )
        salvou(pagina, f)
        print_(pagina, "comercial-criado")

    @op("adwatch", "editar comercial")
    def _():
        pagina.goto(f"{BASE}/adwatch/commercials", wait_until="networkidle")
        linha(pagina, f"VIVO_FIBRA_300{SUF.upper()}")
        f = form_com(pagina, 'input[name="commercial_id"]')
        f.locator('input[name="duration_expected"]').fill("15")
        f.locator('input[name="language"]').fill("pt-BR")
        salvou(pagina, f)

    @op("adwatch", "importar catalogo em lote")
    def _():
        pagina.goto(f"{BASE}/adwatch/commercials", wait_until="networkidle")
        f = form_com(pagina, 'textarea[name="items{}"]')
        f.locator('textarea[name="items{}"]').fill(
            json.dumps(
                [
                    {
                        "commercial_id": f"LOTE_A{SUF.upper()}",
                        "brand": "Marca A",
                        "campaign": f"lote-{RODADA}",
                        "text": "primeira peca do lote de teste",
                    },
                    {
                        "commercial_id": f"LOTE_B{SUF.upper()}",
                        "brand": "Marca B",
                        "campaign": f"lote-{RODADA}",
                        "text": "segunda peca do lote de teste",
                    },
                ],
                ensure_ascii=False,
            )
        )
        salvou(pagina, f, botao="Importar lote")
        texto = pagina.locator("body").inner_text()
        assert f"LOTE_A{SUF.upper()}" in texto, "o lote nao apareceu no catalogo"
        print_(pagina, "lote-importado")

    @op("adwatch", "registrar midia")
    def _():
        pagina.goto(f"{BASE}/adwatch", wait_until="networkidle")
        f = form_com(pagina, '[name="uri"]')
        f.locator('[name="uri"]').fill("/home/user/Downloads/grade-tv-2026-08-26.mp4")
        f.locator('input[name="title"]').fill(f"Grade TV - bloco da noite - rodada {RODADA}")
        salvou(pagina, f)
        print_(pagina, "midia-registrada")

    @op("adwatch", "importar transcricao colada")
    def _():
        pagina.goto(f"{BASE}/adwatch", wait_until="networkidle")
        midia_da_rodada(pagina).locator("details", has_text="Importar transcrição").first.click()
        f = midia_da_rodada(pagina).locator('form:has(textarea[name="payload"])').first
        f.locator('textarea[name="payload"]').fill(transcricao(122.0, 136.7, FALAS))
        salvou(pagina, f)
        print_(pagina, "transcricao-colada")

    @op("adwatch", "importar transcricao por arquivo")
    def _():
        arquivo = pathlib.Path(f"/tmp/e2e/transcricao-r{RODADA}.json")
        arquivo.write_text(transcricao(122.0, 136.7, FALAS), encoding="utf-8")
        pagina.goto(f"{BASE}/adwatch", wait_until="networkidle")
        midia_da_rodada(pagina).locator("details", has_text="Importar transcrição").first.click()
        f = midia_da_rodada(pagina).locator('form:has(input[type="file"])').first
        f.locator('input[type="file"]').set_input_files(str(arquivo))
        salvou(pagina, f)

    @op("adwatch", "detectar comerciais")
    def _():
        pagina.goto(f"{BASE}/adwatch", wait_until="networkidle")
        midia_da_rodada(pagina).locator("details", has_text="Detectar comerciais").first.click()
        f = midia_da_rodada(pagina).locator('form:has(input[name="window_sizes[]"])').first
        f.locator('input[name="window_sizes[]"]').fill("15, 20, 30")
        salvou(pagina, f, botao="Detectar")
        print_(pagina, "deteccao-disparada")

    @op("adwatch", "ingestao automatica (sem FFmpeg)")
    def _():
        pagina.goto(f"{BASE}/adwatch", wait_until="networkidle")
        midia_da_rodada(pagina).locator("details", has_text="Detectar comerciais").first.click()
        f = (
            midia_da_rodada(pagina)
            .locator('form:has(button:has-text("Executar ingestão automática"))')
            .first
        )
        with pagina.expect_navigation(wait_until="networkidle"):
            f.locator('button[type="submit"]').first.click()
        # Sem adaptador de midia isto tem que falhar COM TELA, nunca com JSON cru.
        if "/api/" in pagina.url:
            assert "Erro" in pagina.locator("body").inner_text(), "erro sem moldura"
        print_(pagina, "ingestao-automatica")

    @op("adwatch", "aceitar deteccao")
    def _():
        antes = api("/api/v1/adwatch/detections?limit=100")["items"]
        pendentes = [d for d in antes if d["status"] == "needs_review"]
        assert pendentes, "nao ha deteccao em revisao para aceitar"
        # Lista FILTRADA por "em revisao": agora que as deteccoes aceitas
        # sobrevivem as rodadas seguintes, a primeira linha da lista completa
        # pode ja estar aceita — e o botao Aceitar vem `disabled`, o que so
        # produz um timeout sem explicar nada.
        pagina.goto(f"{BASE}/adwatch/detections?status=needs_review", wait_until="networkidle")
        f = form_com(pagina, 'input[name="status"][value="accepted"]')
        # Guarda o id do que foi aceito: o botao Rejeitar so fica desabilitado
        # quando a linha JA esta rejeitada, entao uma linha aceita continua
        # rejeitavel. Sem esta memoria, o passo seguinte rejeitava a mesma linha
        # e o banco terminava sem nenhuma deteccao aceita.
        aceita_id.append((f.get_attribute("action") or "").rsplit("/", 1)[-1])
        with pagina.expect_navigation(wait_until="networkidle"):
            f.locator('button[type="submit"]').first.click()
        assert "/api/" not in pagina.url, f"aceitar caiu em {pagina.url}"
        depois = api("/api/v1/adwatch/detections?limit=100")["items"]
        aceitas = [d for d in depois if d["status"] == "accepted"]
        assert aceitas, "o clique em Aceitar nao mudou o desfecho de nenhuma deteccao"
        print_(pagina, "deteccao-aceita")

    @op("adwatch", "rejeitar deteccao")
    def _():
        pagina.goto(f"{BASE}/adwatch/detections?status=needs_review", wait_until="networkidle")
        # Outra linha, nao a que acabou de ser aceita.
        formularios = pagina.locator('form:has(input[name="status"][value="rejected"])')
        alvo = None
        for i in range(formularios.count()):
            candidato = formularios.nth(i)
            identificador = (candidato.get_attribute("action") or "").rsplit("/", 1)[-1]
            # O botao vem `disabled` quando a linha JA esta rejeitada; clicar nele
            # so espera o tempo todo e estoura por timeout, sem dizer o motivo.
            if (
                identificador
                and identificador not in aceita_id
                and candidato.locator('button[type="submit"]').first.is_enabled()
            ):
                alvo = candidato
                break
        if alvo is None:
            # A pagina de deteccoes pagina em 25 e a rodada acabou de aceitar a
            # unica candidata visivel. Filtrar por "em revisao" traz as outras.
            pagina.goto(f"{BASE}/adwatch/detections?status=needs_review", wait_until="networkidle")
            formularios = pagina.locator('form:has(input[name="status"][value="rejected"])')
            for i in range(formularios.count()):
                candidato = formularios.nth(i)
                identificador = (candidato.get_attribute("action") or "").rsplit("/", 1)[-1]
                if identificador and identificador not in aceita_id:
                    alvo = candidato
                    break
        assert alvo is not None, "nao ha outra deteccao em revisao para rejeitar"
        with pagina.expect_navigation(wait_until="networkidle"):
            alvo.locator('button[type="submit"]').first.click()
        assert "/api/" not in pagina.url, f"rejeitar caiu em {pagina.url}"
        depois = api("/api/v1/adwatch/detections?limit=100")["items"]
        assert [d for d in depois if d["status"] == "rejected"], (
            "o clique em Rejeitar nao mudou o desfecho de nenhuma deteccao"
        )

    @op("adwatch", "remover comercial descartavel")
    def _():
        # Alvo nomeado, nao "a primeira linha": o catalogo criado nas rodadas fica
        # gravado para consulta, e so a peca `LOTE_B` existe para ser removida.
        codigo = f"LOTE_B{SUF.upper()}"
        assert api(f"/api/v1/adwatch/commercials?search={codigo}")["total"] == 1
        # Filtra antes: o catalogo pagina em 25 e ordena por codigo, entao
        # `LOTE_B-R20` cai na segunda pagina assim que o catalogo cresce.
        pagina.goto(f"{BASE}/adwatch/commercials?q={codigo}", wait_until="networkidle")
        alvo = pagina.locator("tbody tr").filter(has_text=codigo).first
        assert alvo.count(), f"a busca por {codigo} nao trouxe a linha"
        with pagina.expect_navigation(wait_until="networkidle"):
            alvo.locator('button:has-text("Remover")').first.click()
        assert "/api/" not in pagina.url, f"remover caiu em {pagina.url}"
        assert api(f"/api/v1/adwatch/commercials?search={codigo}")["total"] == 0, (
            f"{codigo} continua no catalogo depois do clique em Remover"
        )

    # ------------------------------------------------------------- IDENTIDADE
    @op("identidade", "cadastrar usuario")
    def _():
        pagina.goto(f"{BASE}/identity", wait_until="networkidle")
        f = form_com(pagina, 'input[name="password"]')
        f.locator('input[name="name"]').fill(f"Operador {RODADA}")
        f.locator('input[name="email"]').fill(f"operador{SUF}@exemplo.com")
        f.locator('select[name="role"]').select_option("operator")
        f.locator('input[name="password"]').fill("senha-bem-comprida-de-teste-123")
        salvou(pagina, f)
        print_(pagina, "usuario-cadastrado")

    @op("identidade", "criar chave de API")
    def _():
        pagina.goto(f"{BASE}/identity", wait_until="networkidle")
        f = form_com(pagina, 'input[name="expires_at"]')
        f.locator('input[name="name"]').fill(f"chave-integracao{SUF}")
        f.locator('select[name="role"]').select_option("operator")
        salvou(pagina, f)
        print_(pagina, "chave-criada")

    @op("identidade", "rotacionar chave de API")
    def _():
        pagina.goto(f"{BASE}/identity", wait_until="networkidle")
        antes = [
            k
            for k in api("/api/v1/identity/api-keys")["items"]
            if k["name"] == f"chave-integracao{SUF}"
        ]
        assert antes, "a chave a rotacionar nao existe"
        prefixo_antes = antes[0]["prefix"]
        pagina.goto(f"{BASE}/identity", wait_until="networkidle")
        alvo = pagina.locator("tbody tr").filter(has_text=f"chave-integracao{SUF}").first
        with pagina.expect_navigation(wait_until="networkidle"):
            alvo.locator('button:has-text("Rotacionar")').first.click()
        assert "/api/" not in pagina.url, f"rotacionar caiu em {pagina.url}"
        depois = [
            k
            for k in api("/api/v1/identity/api-keys")["items"]
            if k["name"] == f"chave-integracao{SUF}"
        ]
        assert depois, "a chave sumiu depois da rotacao"
        # Rotacionar emite um segredo novo, e o prefixo publico acompanha. Nao ha
        # campo `rotated_at`: o prefixo E a evidencia de que a chave anterior caiu.
        assert depois[0]["prefix"] != prefixo_antes, (
            "o clique em Rotacionar nao trocou o prefixo: a chave anterior continua valendo"
        )

    @op("identidade", "remover usuario descartavel")
    def _():
        pagina.goto(f"{BASE}/identity", wait_until="networkidle")
        f = form_com(pagina, 'input[name="password"]')
        f.locator('input[name="name"]').fill("Descartavel")
        f.locator('input[name="email"]').fill(f"descartavel{SUF}@exemplo.com")
        f.locator('input[name="password"]').fill("senha-bem-comprida-de-teste-123")
        salvou(pagina, f)
        email = f"descartavel{SUF}@exemplo.com"
        assert any(u["email"] == email for u in api("/api/v1/identity/users")["items"])
        alvo = pagina.locator("tbody tr").filter(has_text=email).first
        with pagina.expect_navigation(wait_until="networkidle"):
            alvo.locator('button[type="submit"]').first.click()
        assert not any(u["email"] == email for u in api("/api/v1/identity/users")["items"]), (
            f"{email} continua cadastrado depois do clique em Remover"
        )

    # ----------------------------------------------------------------- FINOPS
    @op("finops", "criar orcamento")
    def _():
        pagina.goto(f"{BASE}/finops", wait_until="networkidle")
        f = form_com(pagina, 'input[name="limit_usd"]')
        f.locator('input[name="name"]').fill(f"Teto mensal AdWatch {RODADA}")
        f.locator('input[name="limit_usd"]').fill("250")
        f.locator('select[name="period"]').select_option("monthly")
        f.locator('input[name="alert_threshold"]').fill("0.8")
        salvou(pagina, f, botao="Criar orçamento")
        print_(pagina, "orcamento-criado")

    # --------------------------------------------------------------- REGISTRY
    @op("registry", "redescobrir building blocks")
    def _():
        pagina.goto(f"{BASE}/registry", wait_until="networkidle")
        alvo = pagina.locator('form[action="/api/v1/registry/discover"]').first
        with pagina.expect_navigation(wait_until="networkidle"):
            alvo.locator('button[type="submit"]').first.click()
        assert "/api/" not in pagina.url, f"discover caiu em {pagina.url}"
        print_(pagina, "registry-descoberto")

    # ------------------------------------------------------- telas de leitura
    for caminho, nome in [
        ("/", "cockpit"),
        ("/runs", "execucoes"),
        ("/finops", "finops"),
        ("/observability", "observabilidade"),
        ("/settings", "configuracoes"),
        ("/adwatch/detections", "deteccoes"),
    ]:
        pagina.goto(f"{BASE}{caminho}", wait_until="networkidle")
        print_(pagina, nome)

    contexto.close()
    navegador.close()

# ---------------------------------------------------------------- relatorio
falhas = [(a, o, m) for a, o, m in resultados if m != "ok"]
print(f"\n=== RODADA {RODADA}: {len(resultados) - len(falhas)}/{len(resultados)} operacoes ok ===")
for area, operacao, motivo in falhas:
    print(f"  FALHA  {area}/{operacao}\n         {motivo}")
pathlib.Path(f"/tmp/e2e/resultado-r{RODADA}.json").write_text(
    json.dumps(
        [{"area": a, "operacao": o, "desfecho": m} for a, o, m in resultados],
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
sys.exit(1 if falhas else 0)
