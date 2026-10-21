#!/usr/bin/env bash
# Run the whole event-driven stack locally: API (kafka mode) + relay + 2 validators + notifier.
# Ctrl+C stops everything gracefully (SIGTERM → workers finish, commit, leave the group).
#
#   ./scripts/dev_stack.sh            # then POST receipts at http://127.0.0.1:8000/docs
#   tail -f .stack-logs/*.log         # in another terminal, to watch partitions / rebalances
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up -d --wait
LOGS=.stack-logs
mkdir -p "$LOGS"

pids=()
start() { local name=$1; shift; "$@" >"$LOGS/$name.log" 2>&1 & pids+=($!); echo "started $name (pid $!) → $LOGS/$name.log"; }

export VALIDATION_MODE=kafka
start api       uv run fastapi run app/main.py --port 8000
start relay     uv run python -m app.workers.relay
start w1        uv run python -m app.workers.validator --name w1
start w2        uv run python -m app.workers.validator --name w2
start notifier  uv run python -m app.workers.notifier

trap 'echo; echo "stopping…"; kill -TERM "${pids[@]}" 2>/dev/null; wait; echo "stopped"' INT TERM
echo "stack running — Ctrl+C to stop"
wait
