# =============================================================================
# lukato 1.0.0 — imagem de producao
# Multi-stage: builder (wheels) -> runtime enxuto, non-root, pronto p/ Kubernetes
# =============================================================================

# ------------------------------ estagio: builder -----------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps .

# ------------------------------ estagio: runtime -----------------------------
FROM python:3.11-slim-bookworm AS runtime

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

# curl: probes do Kubernetes / healthcheck do compose
# libpq5: cliente PostgreSQL usado por asyncpg em alguns caminhos
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libpq5 tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 lukato \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin lukato

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=10001:10001 src ./src
COPY --chown=10001:10001 migrations ./migrations
COPY --chown=10001:10001 alembic.ini pyproject.toml README.md ./
COPY --chown=10001:10001 scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /app/var && chown -R 10001:10001 /app/var

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${LUKATO_APP__PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
