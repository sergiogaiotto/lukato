# =============================================================================
# lukato 1.0.0 — imagem de producao
# Multi-stage: builder (wheels) -> runtime enxuto, non-root, pronto p/ Kubernetes
#
# NENHUM `apt-get` — de proposito. A imagem nao depende de alcancar um espelho
# Debian, o que a torna construivel em rede fechada e tira ~200 MB de toolchain
# e dois pacotes de sistema da conta. O que cada um deles fazia aqui e por que
# saiu esta escrito abaixo, no ponto em que era instalado.
# =============================================================================

# Imagem base parametrizavel: por padrao a oficial do Docker Hub, mas em rede
# corporativa (ou atras de um proxy que bloqueie o CDN de blobs do Hub) aponte
# para o espelho interno:
#   docker build --build-arg PYTHON_IMAGE=<registry>/python:3.11-slim-bookworm .
# Espelhos publicos que funcionam quando o CDN do Hub nao responde:
#   mirror.gcr.io/library/python:3.11-slim-bookworm
#   public.ecr.aws/docker/library/python:3.11-slim-bookworm
# Serve qualquer imagem com Python 3.11 e `useradd` — nao precisa mais ser Debian.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm

# tini como PID 1, baixado como binario ESTATICO e conferido por checksum, em vez
# de vir do apt. Rede fechada: aponte TINI_URL para o espelho interno. O build
# QUEBRA se o sha256 nao bater — nao ha caminho silencioso aqui.
ARG TINI_VERSION=v0.19.0
ARG TINI_URL=https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-amd64
ARG TINI_SHA256=c5b0666b4cb676901f90dfcb37106783c5fe2077b04590973b885950611b30ee

# Capacidade multimodal OPCIONAL (AdWatch processando arquivos de video de
# verdade: transcricao com timestamps por palavra, cortes de cena, sondagem):
#   docker build --build-arg WITH_MEDIA=1 .
# Instala FFmpeg/FFprobe estaticos (mesmo padrao do tini: URL + sha256, o build
# quebra se nao conferir) e o subset CPU de requirements-media-image.txt.
# Sem o arg a imagem continua enxuta como sempre foi — e o console mostra as
# capacidades ausentes e como habilita-las.
ARG WITH_MEDIA=0
ARG FFBIN_FFMPEG_URL=https://github.com/vot/ffbinaries-prebuilt/releases/download/v6.1/ffmpeg-6.1-linux-64.zip
ARG FFBIN_FFMPEG_SHA256=8bb4a27f5fd02f3dd9a5e75c9eddf6ace1d50a08929ee0d20bbf17eb467fb711
ARG FFBIN_FFPROBE_URL=https://github.com/vot/ffbinaries-prebuilt/releases/download/v6.1/ffprobe-6.1-linux-64.zip
ARG FFBIN_FFPROBE_SHA256=cb690c360042b51d9e901db2b0185c585330c1067b5c5edf0b6a5e26e0375e2a

# ------------------------------ estagio: builder -----------------------------
FROM ${PYTHON_IMAGE} AS builder

ARG TINI_URL
ARG TINI_SHA256
ARG WITH_MEDIA
ARG FFBIN_FFMPEG_URL
ARG FFBIN_FFMPEG_SHA256
ARG FFBIN_FFPROBE_URL
ARG FFBIN_FFPROBE_SHA256

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Aqui havia `apt-get install build-essential`. Saiu porque nao era necessario:
# os 99 pacotes de requirements.txt publicam wheel binario para cp311 linux
# x86_64 (conferido com `pip download --only-binary=:all:`), entao nada compila.
# Se um dia algum pacote passar a exigir compilacao, o pip falha com a mensagem
# exata do que faltou — e ai a decisao de reinstalar o toolchain e consciente,
# nao herdada.

# CAs adicionais, para rede com proxy que faz interceptacao TLS. O diretorio e
# versionado com um README e nada mais: sem `.crt` dentro, este passo nao faz nada
# e nao custa camada util. Com um `.crt`, tudo que o build baixa por HTTPS daqui
# em diante passa a confiar nele. Ver deploy/ca/README.md.
COPY deploy/ca /tmp/ca-extra
RUN set -eu; \
    origem="$(python -c 'import ssl; print(ssl.get_default_verify_paths().cafile or "")')"; \
    if ls /tmp/ca-extra/*.crt >/dev/null 2>&1; then \
      cat "$origem" /tmp/ca-extra/*.crt > /opt/ca-bundle.crt; \
      echo "CA extra acrescentada: $(ls /tmp/ca-extra/*.crt | tr '\n' ' ')"; \
    else \
      cp "$origem" /opt/ca-bundle.crt; \
    fi
ENV SSL_CERT_FILE=/opt/ca-bundle.crt \
    REQUESTS_CA_BUNDLE=/opt/ca-bundle.crt \
    PIP_CERT=/opt/ca-bundle.crt

COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install --only-binary=:all: -r requirements.txt

# tini: baixado com o proprio Python da imagem (nao ha curl, e nao precisa haver)
# e verificado antes de virar executavel.
RUN python -c "\
import hashlib, sys, urllib.request; \
alvo = '/opt/tini'; \
urllib.request.urlretrieve('${TINI_URL}', alvo); \
dado = open(alvo, 'rb').read(); \
achado = hashlib.sha256(dado).hexdigest(); \
esperado = '${TINI_SHA256}'; \
sys.exit(0) if achado == esperado else sys.exit( \
    'tini: sha256 nao confere\n  esperado: ' + esperado + '\n  obtido:   ' + achado + \
    '\nSe voce apontou TINI_URL para um espelho, ajuste TINI_SHA256 junto.')" \
 && chmod +x /opt/tini \
 && /opt/tini --version

# Capacidade multimodal (WITH_MEDIA=1). Tres decisoes deliberadas aqui:
#
#   /opt/ffbin existe SEMPRE (vazio + LEIA-ME na imagem enxuta) para o COPY do
#   runtime nao depender do build-arg. FFmpeg/FFprobe sao binarios ESTATICOS,
#   extraidos com o zipfile do proprio Python (sem unzip, sem apt) e conferidos
#   por sha256 antes de ganhar bit de execucao — o padrao do tini, acima.
#
#   torch/torchaudio vem do indice CPU do PyTorch (--extra-index-url): os pins
#   `+cpu` de requirements-media-image.txt so existem la, entao trocar o indice
#   por engano quebra o build em vez de baixar ~6 GB de CUDA em silencio.
#
#   antlr4-python3-runtime e a UNICA excecao ao --only-binary: a versao que o
#   omegaconf (via pyannote-audio) exige so publica sdist. E Python puro —
#   compila com o setuptools que ja esta no venv, sem toolchain de sistema.
COPY requirements-media-image.txt ./
RUN set -eu; \
    mkdir -p /opt/ffbin; \
    if [ "${WITH_MEDIA}" = "1" ]; then \
      for espec in "ffmpeg ${FFBIN_FFMPEG_URL} ${FFBIN_FFMPEG_SHA256}" \
                   "ffprobe ${FFBIN_FFPROBE_URL} ${FFBIN_FFPROBE_SHA256}"; do \
        set -- $espec; \
        python -c "\
import hashlib, sys, urllib.request, zipfile; \
nome, url, esperado = sys.argv[1], sys.argv[2], sys.argv[3]; \
alvo = '/tmp/' + nome + '.zip'; \
urllib.request.urlretrieve(url, alvo); \
achado = hashlib.sha256(open(alvo, 'rb').read()).hexdigest(); \
sys.exit(0) if achado == esperado else sys.exit( \
    nome + ': sha256 nao confere\n  esperado: ' + esperado + '\n  obtido:   ' + achado + \
    '\nSe voce apontou a URL para um espelho, ajuste o SHA256 junto.')" \
          "$1" "$2" "$3"; \
        python -c "\
import sys, zipfile; \
zipfile.ZipFile('/tmp/' + sys.argv[1] + '.zip').extract(sys.argv[1], '/opt/ffbin')" "$1"; \
        chmod 0755 "/opt/ffbin/$1"; \
        "/opt/ffbin/$1" -version > "/tmp/$1-versao.txt"; \
        head -1 "/tmp/$1-versao.txt"; \
        rm -f "/tmp/$1.zip" "/tmp/$1-versao.txt"; \
      done; \
      /opt/venv/bin/pip install --only-binary=:all: --no-binary antlr4-python3-runtime \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements-media-image.txt; \
    else \
      printf '%s\n' 'Imagem construida sem WITH_MEDIA=1: nao ha ffmpeg/ffprobe aqui.' \
        'Reconstrua com `docker build --build-arg WITH_MEDIA=1 .` para habilitar.' \
        > /opt/ffbin/LEIA-ME.txt; \
    fi

COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps .

# ------------------------------ estagio: runtime -----------------------------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="lukato" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.description="Ecossistema modular de agentes de IA (hexagonal, guardrails parametrizaveis)" \
      org.opencontainers.image.licenses="Proprietary"

# /opt/ffbin entra no PATH mesmo na imagem enxuta: vazio, nao muda nada; com
# WITH_MEDIA=1, poe ffmpeg/ffprobe onde os adaptadores de midia procuram.
# HF_HOME/TORCH_HOME ficam sob /app/var de proposito: e o diretorio que o
# docker-compose monta como volume, entao os modelos de ASR/alinhamento sao
# baixados UMA vez e sobrevivem a `--force-recreate` — sem isso, cada recreate
# baixaria centenas de MB de novo no primeiro clique de ingestao.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:/opt/ffbin:$PATH" \
    HF_HOME=/app/var/hf \
    TORCH_HOME=/app/var/torch \
    LUKATO_APP__HOST=0.0.0.0 \
    LUKATO_APP__PORT=8000

# Aqui havia `apt-get install curl libpq5 tini`. Os tres sairam:
#
#   curl    servia so ao HEALTHCHECK. Trocado pelo Python que ja esta na imagem
#           (ver o HEALTHCHECK no fim do arquivo) — um pacote a menos e uma
#           superficie de CVE a menos, pelo mesmo resultado.
#   libpq5  nao era usado por ninguem. asyncpg implementa o protocolo de fio do
#           PostgreSQL em C proprio e nao linka libpq: `ldd` em todos os .so do
#           ambiente devolve zero referencias. O comentario antigo dizia que
#           asyncpg precisava "em alguns caminhos"; nao precisa em nenhum.
#   tini    continua sendo PID 1, mas agora vem do estagio builder como binario
#           estatico verificado por sha256.
#
# `groupadd`/`useradd` vem da propria imagem base, nao do apt.
RUN groupadd --gid 10001 lukato \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin lukato

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/tini /usr/bin/tini
COPY --from=builder /opt/ffbin /opt/ffbin
COPY --chown=10001:10001 src ./src
COPY --chown=10001:10001 migrations ./migrations
COPY --chown=10001:10001 alembic.ini pyproject.toml README.md ./
COPY --chown=10001:10001 scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

# `tr -d '\r'` antes do chmod: o `.gitattributes` impede que o checkout no
# Windows estrague o script, mas quem clonou ANTES dele ja tem o arquivo com CRLF
# no disco, e o build usa o que esta no disco. Sem esta linha o container morre no
# start com `/usr/bin/env: 'bash\r': No such file or directory` — uma mensagem que
# nao menciona final de linha em lugar nenhum, e que custa horas para quem nunca
# viu. Um `tr` idempotente e barato demais para deixar essa armadilha de pe.
RUN tr -d '\r' < /usr/local/bin/entrypoint.sh > /tmp/entrypoint.sh \
 && mv /tmp/entrypoint.sh /usr/local/bin/entrypoint.sh \
 && chmod +x /usr/local/bin/entrypoint.sh \
 && head -1 /usr/local/bin/entrypoint.sh | grep -q '^#!/usr/bin/env bash$' \
 && mkdir -p /app/var && chown -R 10001:10001 /app/var

USER 10001:10001

EXPOSE 8000

# Sem curl: o proprio Python sonda /healthz. Codigo != 200 ou qualquer excecao
# (conexao recusada, timeout) sai diferente de zero, que e o que o Docker le.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request;\
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('LUKATO_APP__PORT','8000')+'/healthz', timeout=4).status==200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
