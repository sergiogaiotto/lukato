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

# ------------------------------ estagio: builder -----------------------------
FROM ${PYTHON_IMAGE} AS builder

ARG TINI_URL
ARG TINI_SHA256

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

COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps .

# ------------------------------ estagio: runtime -----------------------------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="lukato" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.description="Ecossistema modular de agentes de IA (hexagonal, guardrails parametrizaveis)" \
      org.opencontainers.image.licenses="Proprietary"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
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
COPY --chown=10001:10001 src ./src
COPY --chown=10001:10001 migrations ./migrations
COPY --chown=10001:10001 alembic.ini pyproject.toml README.md ./
COPY --chown=10001:10001 scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
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
