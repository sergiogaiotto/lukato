# fixtures

## `demo-export.json`

O que uma instalação acumulou depois de 22 rodadas da navegação fim a fim
(`scripts/navegacao_fim_a_fim.py`): 55 prompts, 27 guardrails, 23 módulos, 55
comerciais e 29 mídias. Serve para encher um console vazio com algo que se
pareça com uso real — a `seed` planta o mínimo, isto planta um dia de trabalho.

Carregar:

    lukato import fixtures/demo-export.json

ou, sem copiar arquivo para dentro do contêiner:

    docker compose exec -T api lukato import - < fixtures/demo-export.json

É idempotente: o que já existe pelo identificador é mantido, não sobrescrito.

## O que NÃO está aqui, e por quê

* **Segredo de chave de API e hash de senha.** Um arquivo de fixture vive num
  repositório, e repositório é o último lugar onde um segredo deve estar. O
  `export` nunca os emite — conferido antes de versionar este arquivo.
* **Execuções e detecções.** São derivadas. Importar uma detecção criaria numa
  instalação a evidência de um cálculo que nunca aconteceu ali, que é o oposto
  do que a trilha de auditoria significa. Elas voltam rodando o funil.
* **Histórico de versão de prompt.** O export lista as versões que existem na
  origem; a importação cria a primeira versão de cada slug. As 55 linhas de
  prompt daqui cobrem menos slugs, e cada um chega em v1.

As mídias estão listadas mas **não são recriadas** na importação: `uri` aponta
para um caminho da máquina de origem, e registrar um caminho que não existe cria
um ativo que nenhuma etapa consegue ler.
