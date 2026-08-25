#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# lukato — entrypoint do container
#   serve    (padrao) sobe a API + console com uvicorn
#   migrate  aplica as migracoes Alembic e sai
#   seed     popula dados de demonstracao e sai
#   worker   reservado para processamento assincrono
#   shell    shell python com o container de dependencias montado
# -----------------------------------------------------------------------------
set -euo pipefail

CMD="${1:-serve}"
shift || true

HOST="${LUKATO_APP__HOST:-0.0.0.0}"
PORT="${LUKATO_APP__PORT:-8000}"
WORKERS="${LUKATO_APP__WORKERS:-1}"
LOG_LEVEL="$(echo "${LUKATO_OBSERVABILITY__LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

run_migrations() {
  if [ "${LUKATO_DB__RUN_MIGRATIONS:-true}" = "true" ]; then
    log "aplicando migracoes Alembic..."
    alembic upgrade head || log "AVISO: migracoes falharam; seguindo com create_all se habilitado"
  else
    log "migracoes desabilitadas (LUKATO_DB__RUN_MIGRATIONS=false)"
  fi
}

case "$CMD" in
  serve)
    run_migrations
    log "iniciando lukato em ${HOST}:${PORT} (workers=${WORKERS})"
    exec uvicorn lukato.main:app \
      --host "$HOST" --port "$PORT" --workers "$WORKERS" \
      --log-level "$LOG_LEVEL" --proxy-headers --forwarded-allow-ips='*' "$@"
    ;;
  migrate)  exec alembic upgrade head "$@" ;;
  seed)     exec python -m lukato.interfaces.cli seed "$@" ;;
  worker)   log "worker ainda nao implementado nesta versao"; exit 1 ;;
  shell)    exec python "$@" ;;
  *)        exec "$CMD" "$@" ;;
esac
