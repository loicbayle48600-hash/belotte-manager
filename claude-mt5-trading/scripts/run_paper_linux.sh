#!/usr/bin/env bash
# Session "papier" hors Windows : broker simulé (TRADINGLAB_BROKER=mock), watchdog + orchestrateur + dashboard.
# Sert à valider la boucle, les gardes et le dashboard sans MetaTrader 5. Aucun ordre réel n'est envoyé.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
export TRADINGLAB_HOME="${TRADINGLAB_HOME:-$HERE}"
export TRADINGLAB_BROKER=mock
export PYTHONPATH="$HERE/src"
MODE="${1:-SAFE}"
mkdir -p "$HERE/logs" "$HERE/state"
python3 -m tradinglab.monitoring.watchdog --broker mock > "$HERE/logs/watchdog.out.log" 2>&1 &
WD=$!
python3 -m tradinglab.dashboards.server --home "$HERE" > "$HERE/logs/dashboard.out.log" 2>&1 &
DB=$!
echo "watchdog PID $WD, dashboard PID $DB (http://127.0.0.1:8765) — Ctrl+C pour arrêter"
trap 'kill $WD $DB 2>/dev/null || true' EXIT
python3 -m tradinglab.orchestration.orchestrator --broker mock --mode "$MODE" --home "$HERE"
