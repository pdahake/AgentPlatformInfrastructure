#!/usr/bin/env bash
# Exercises the full stack end-to-end by calling the already-running `agent`
# service directly over HTTP — no container spin-up per call. Run this after
# `docker compose up`; the agent service stays up the whole time, so you can
# call this repeatedly with different tasks without re-ingesting anything or
# paying container-startup cost per call.
#
# Usage:
#   ./scripts/run_task.sh "some task" [date_from] [date_to] [model]
#   ./scripts/run_task.sh "Summarize news about oil and airlines in early 2015, and relate that to how AAL traded that period" 2015-01-01 2015-03-31
#
# Env:
#   AGENT_URL  — default http://localhost:8000 (set to http://dev01.lab.home.arpa:8000
#                or similar if calling from another machine on the network)
set -euo pipefail

AGENT_URL="${AGENT_URL:-http://localhost:8000}"
JAEGER_URL="${JAEGER_URL:-http://localhost:16686}"

if [ $# -lt 1 ]; then
    echo "Usage: $0 \"<task>\" [date_from] [date_to] [model]" >&2
    exit 1
fi

task="$1"
date_from="${2:-}"
date_to="${3:-}"
model="${4:-}"

if ! command -v jq >/dev/null 2>&1; then
    echo "jq is required (used to safely JSON-encode the task text)" >&2
    exit 1
fi

payload=$(jq -n \
    --arg task "$task" \
    --arg date_from "$date_from" \
    --arg date_to "$date_to" \
    --arg model "$model" \
    '{task: $task,
      date_from: (if $date_from == "" then null else $date_from end),
      date_to: (if $date_to == "" then null else $date_to end),
      model: (if $model == "" then null else $model end)}')

response_file=$(mktemp)
trap 'rm -f "$response_file"' EXIT

echo "--> POST $AGENT_URL/run" >&2

http_code=$(curl -s -o "$response_file" -w '%{http_code}' \
    -X POST "$AGENT_URL/run" \
    -H 'content-type: application/json' \
    --max-time 180 \
    -d "$payload") || {
    echo "could not reach agent service at $AGENT_URL — is 'docker compose up' running?" >&2
    exit 1
}

if [ "$http_code" != "200" ]; then
    echo "agent returned HTTP $http_code:" >&2
    cat "$response_file" >&2
    exit 1
fi

jq . "$response_file"
trace_id=$(jq -r .trace_id "$response_file")

echo "" >&2
echo "Trace: $JAEGER_URL/trace/$trace_id" >&2
