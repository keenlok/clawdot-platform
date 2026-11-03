#!/usr/bin/env bash
set -euo pipefail
APP_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
COMPOSE=(docker compose --project-directory "$APP_DIR" -f "$APP_DIR/compose.yaml")
case "${1:-run}" in
  build) "${COMPOSE[@]}" build ;;
  run) "${COMPOSE[@]}" run --rm example-bot ;;
  *) echo 'Usage: deploy.sh [build|run]' >&2; exit 2 ;;
esac
