"""`lukato export` e `lukato import`: o par que leva uma instalacao para outra.

Um banco de demonstracao morre com o ambiente que o hospeda. Estes dois comandos
existem para que prompts, guardrails, modulos e catalogo montados numa maquina
possam ser consultados em outra — e o que eles NAO carregam e tao importante
quanto o que carregam.

Os testes daqui nao abrem banco: exercitam a borda da CLI, que e onde os erros
que o usuario ve de verdade acontecem — arquivo errado, formato errado, e a
escolha entre ler de um caminho ou da entrada padrao.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lukato.interfaces.cli import main

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/demo-export.json"


def test_a_fixture_de_demonstracao_e_um_export_valido() -> None:
    """`fixtures/demo-export.json` precisa continuar carregavel por `import`.

    Ela existe para encher um console vazio com algo parecido com uso real. Um
    arquivo de fixture que deixou de casar com o formato so se descobre no dia em
    que alguem tenta usa-lo — normalmente numa demonstracao.
    """
    dados = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert dados["lukato_export"] == 1
    for chave in ("prompts", "guardrails", "modules", "commercials", "media"):
        assert isinstance(dados[chave], list), f"{chave} deveria ser lista"
    assert dados["prompts"], "a fixture nao teria valor vazia"


def test_a_fixture_nao_carrega_segredo() -> None:
    """Fixture vive em repositorio; segredo nao pode viver em repositorio.

    O `export` ja e escrito para nao emitir segredo de chave nem hash de senha.
    Esta trava confere o RESULTADO, e nao a intencao: se um campo novo passar a
    ser exportado carregando credencial junto, o teste cai aqui e nao no dia do
    vazamento.
    """
    bruto = FIXTURE.read_text(encoding="utf-8")
    for campo in ('"secret"', '"password"', '"password_hash"', '"api_key"'):
        assert campo not in bruto.lower(), f"a fixture carrega o campo {campo}"

    # Formato exato do segredo, e nao um pedaco solto: `lk_` casa dentro de
    # `TESTE_BULK_1`, e um teste que grita por causa do codigo de um comercial
    # ensina a ignora-lo. Uma chave e `lk_<8 do prefixo>_<corpo>`.
    for formato in (r"lk_[a-z0-9]{8}_[A-Za-z0-9_-]{8,}", r"sk-[A-Za-z0-9]{16,}"):
        achado = re.search(formato, bruto)
        assert achado is None, f"a fixture carrega o que parece um segredo: {achado!r}"


def test_arquivo_inexistente_devolve_uma_linha_e_nao_um_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Caminho errado e falha de ambiente: merece uma frase, nao uma pilha.

    Antes deste ramo a CLI despejava doze linhas terminando em
    `FileNotFoundError`, e quem lia precisava procurar a causa no meio da pilha —
    num caminho que costuma estar errado por uma letra so.
    """
    codigo = main(["import", "/caminho/que/nao/existe.json"])
    assert codigo == 1
    erro = capsys.readouterr().err
    assert "Traceback" not in erro, f"a CLI ainda despeja pilha:\n{erro}"
    assert "/caminho/que/nao/existe.json" in erro
    assert "No such file" in erro or "nao existe" in erro


def test_json_que_nao_e_export_diz_o_que_falta(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Recusar e barato; recusar dizendo o motivo e o que evita a segunda tentativa."""
    intruso = tmp_path / "outra-coisa.json"
    intruso.write_text('{"foo": 1}', encoding="utf-8")
    assert main(["import", str(intruso)]) == 1
    erro = capsys.readouterr().err
    assert "lukato_export" in erro
    assert str(intruso) in erro


def test_entrada_padrao_e_aceita_com_hifen(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-` le de stdin — o caminho de quem roda de fora do contentor.

    Sem isto o arquivo teria de ser copiado para DENTRO do contentor antes, e o
    `import` falhava com `FileNotFoundError` apontando um caminho de dentro
    enquanto o arquivo estava do lado de fora: um erro que nao diz onde esta o
    problema. Aqui mandamos um JSON invalido de proposito — o que se mede e que
    ele foi LIDO da entrada padrao, e a recusa cita a entrada padrao, nao um
    caminho de arquivo.
    """
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO('{"foo": 1}'))
    assert main(["import", "-"]) == 1
    erro = capsys.readouterr().err
    assert "entrada padrao" in erro, erro
    assert "lukato_export" in erro
