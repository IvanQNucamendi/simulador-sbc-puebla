#!/usr/bin/env bash
# Arranca el visualizador en local: crea la venv si hace falta, instala
# dependencias solo la primera vez, levanta el servidor y abre el navegador.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creando entorno virtual (.venv)…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

PORT="${1:-8000}"
( sleep 1.5 && open "http://127.0.0.1:${PORT}" 2>/dev/null || true ) &
exec .venv/bin/uvicorn main:app --port "${PORT}"
