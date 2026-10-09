#!/bin/sh
set -eu

# Apply database migrations before starting the bot (idempotent).
if [ "${1:-}" = "python" ] && [ "${2:-}" = "-m" ] && [ "${3:-}" = "app.main" ] && [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  alembic upgrade head
fi

exec "$@"
