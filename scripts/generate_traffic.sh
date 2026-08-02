#!/usr/bin/env bash
# Fires a mix of realistic sample tasks at the already-running `agent` service
# at random intervals, to populate Grafana dashboards/traces/logs/alerts with
# a believable spread of traffic instead of one-off manual calls. Reuses the
# same tasks documented in RUNBOOK.md's "Sample tasks to try" — fundamentals,
# price history, news full-text, news semantic search, cross-ticker
# comparisons — plus a deliberate mix of error-inducing tasks so the error
# panels/alerts (task failure rate, tool error rate, LLM call error rate)
# have real data to show, not just whatever happens to fail organically:
#   - missing-ticker tool errors (TSLA/GOOGL/UBER aren't in `fundamentals` —
#     this is a ~2013-2016-era S&P 500 dataset, confirmed via
#     `SELECT DISTINCT ticker FROM fundamentals`)
#   - a pre-2003 date range for a news search, reliably zero full-text/
#     semantic matches (the corpus starts ~2003)
#   - an invalid `model` override — rejected by litellm at the routing
#     layer before any real generation happens, so it's a clean, ~free way
#     to generate `llm call failed` errors on demand (confirmed: fails in
#     under a second, no token spend)
#
# Usage:
#   ./scripts/generate_traffic.sh [duration_seconds] [max_interval_seconds]
#   ./scripts/generate_traffic.sh              # 1 hour, 0-120s between calls (defaults)
#   ./scripts/generate_traffic.sh 600 30        # 10 minutes, 0-30s between calls
#
# Env:
#   AGENT_URL — passed through to run_task.sh (default http://localhost:8000)
set -uo pipefail
cd "$(dirname "$0")/.."

DURATION_SECONDS="${1:-3600}"
MAX_INTERVAL_SECONDS="${2:-120}"
END=$(( $(date +%s) + DURATION_SECONDS ))

# task text <TAB> date_from <TAB> date_to <TAB> model  (blank fields = default)
tasks=(
$'What was AAPL\'s most recent reported net income and profit margin?\t\t\t'
$'Compare MSFT\'s revenue and profit margin across its last four reported periods. Is the trend improving?\t\t\t'
$'How did XOM stock perform in Q4 2015? Mention the high, low, and overall trend.\t2015-10-01\t2015-12-31\t'
$'What major headlines were reported around the September 2008 financial crisis?\t2008-09-01\t2008-10-31\t'
$'Summarize news about oil and airlines in early 2015, and relate that to how AAL traded that period\t2015-01-01\t2015-03-31\t'
$'Compare AAPL and MSFT: which had stronger revenue growth between 2013 and 2015?\t\t\t'
$'What news headlines discuss oil prices or the energy sector?\t\t\t'
$'What did the news say about the technology sector in 2016?\t2016-01-01\t2016-12-31\t'
$'How did JPM perform financially in 2014, and were there any relevant banking news headlines that year?\t2014-01-01\t2014-12-31\t'
$'What was XOM\'s most recent reported revenue?\t\t\t'
$'Compare AAPL and AAL: which had stronger stock performance in 2015?\t2015-01-01\t2015-12-31\t'
# -- deliberate tool errors: ticker genuinely absent from `fundamentals` --
$'What was TSLA\'s net income in 2015?\t\t\t'
$'What was GOOGL\'s most recent reported revenue?\t\t\t'
$'What was UBER\'s net income in 2015?\t\t\t'
# -- deliberate tool error: date range predates the news corpus --
$'What headlines were published about the economy in 1995?\t1995-01-01\t1995-12-31\t'
# -- deliberate LLM call errors: invalid model, rejected before generation --
$'What was AAPL\'s most recent reported net income?\t\t\tnonexistent-model-xyz'
$'What was MSFT\'s most recent reported revenue?\t\t\tnonexistent-model-xyz'
)

i=0
while [ "$(date +%s)" -lt "$END" ]; do
    entry="${tasks[$((RANDOM % ${#tasks[@]}))]}"
    IFS=$'\t' read -r task date_from date_to model <<< "$entry"
    i=$((i + 1))

    echo "[$(date '+%H:%M:%S')] run #$i: $task ${date_from:+($date_from to $date_to)} ${model:+[model=$model]}"
    ./scripts/run_task.sh "$task" "$date_from" "$date_to" "$model" || echo "  (run #$i failed, continuing)"

    sleep_s=$((RANDOM % (MAX_INTERVAL_SECONDS + 1)))
    echo "[$(date '+%H:%M:%S')] sleeping ${sleep_s}s"
    sleep "$sleep_s"
done

echo "traffic generation complete: $i tasks run over ${DURATION_SECONDS}s"
