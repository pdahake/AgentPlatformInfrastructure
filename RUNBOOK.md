# Runbook

Operational reference for running, verifying, and troubleshooting the agent platform.
For *why* things are built this way, see [DECISIONS.md](DECISIONS.md).

## Contents
- [Architecture at a glance](#architecture-at-a-glance)
- [Prerequisites](#prerequisites)
- [First-time setup](#first-time-setup)
- [Starting the stack](#starting-the-stack)
- [Verifying everything is healthy](#verifying-everything-is-healthy)
- [Running a task](#running-a-task)
- [Accessing the UIs](#accessing-the-uis)
- [Dashboard panels](#dashboard-panels)
- [Viewing logs](#viewing-logs)
- [Alerts](#alerts)
- [Common operations](#common-operations)
- [Troubleshooting](#troubleshooting)
- [Shutting down / resetting](#shutting-down--resetting)

## Architecture at a glance

| Service | Role | Port |
|---|---|---|
| `postgres` | Postgres 16 + pgvector — fundamentals, prices, news_headlines | 5432 |
| `litellm` | Model gateway (Anthropic/OpenAI), holds the real provider keys | 4000 |
| `ingest` | One-shot: loads CSVs / restores export, embeds a headline subset | — |
| `agent` | FastAPI service: tool-calling loop, `/run`, `/health`, `/metrics` | 8000 |
| `otel-collector` | Receives traces from `agent`, forwards to Jaeger | 4317 (internal) |
| `jaeger` | Trace UI | 16686 |
| `prometheus` | Scrapes `agent:8000/metrics` + blackbox probes of every service | 9090 |
| `blackbox-exporter` | TCP/HTTP-probes every service uniformly, for the "a service is down" alert | 9115 |
| `loki` | Log storage — receives every container's stdout from `alloy` | 3100 |
| `alloy` | Discovers every container via the Docker API and ships its logs to Loki | 12346 (own UI, host-remapped — see Troubleshooting if this conflicts) |
| `grafana` | Dashboards + log/trace/metric explore over Prometheus, Jaeger, and Loki | 3000 |

`ingest` runs to completion once per fresh volume (or is a fast restore from `data/export/`, see [Common operations](#common-operations)); `agent` won't start until it exits successfully.

## Prerequisites

- Docker + Docker Compose v2 (`docker compose version`)
- `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` (chat via Claude, embeddings via OpenAI — both proxied through `litellm`)
- Nothing else — `data/raw/{fundamentals,prices,news_headlines}.csv.gz` (the raw Kaggle data, gzipped) and `data/export/{fundamentals,prices,news_headlines}.copy.gz` (a pre-built ingest cache) both ship in the repo, so a fresh clone needs no separate data download step

## First-time setup

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY / OPENAI_API_KEY
# (in this environment: vault kv get secret/ai)
```

Leave everything else in `.env` at its default unless you know you need to change it — see [Common operations](#common-operations) for what each knob does.

## Starting the stack

```bash
docker compose up --build -d
```

This one command:
1. Builds the `agent` and `ingest` images.
2. Starts `postgres` and `litellm`, waits for both to be healthy.
3. Runs `ingest` to completion — first run parses the raw CSVs (~2–4 min including embedding attempts); every run after that restores from `data/export/*.copy.gz` in under a minute (see `DATA_SOURCE=auto` in DECISIONS.md).
4. Starts `agent` and the observability services.

Watch it come up:
```bash
docker compose logs -f ingest      # first-run progress / restore progress
docker compose ps                   # overall status once ingest exits
```

### `docker compose up` flags used in this runbook

| Flag | What it does | Why/when here |
|---|---|---|
| `--build` | Rebuild `agent`/`ingest` images before starting, instead of reusing a stale local image | Needed any time you change code in `agent/` or `ingest/`, or their `Dockerfile`/`requirements.txt`. Harmless (just a cache-hit no-op) if nothing changed. |
| `-d` / `--detach` | Run in the background instead of attaching to combined logs | Default for normal use; drop it (plain `docker compose up`) the first time you stand up the stack if you want to watch everything boot in one stream, `Ctrl-C` stops it all. |
| `--no-deps <service>` | Start only the named service(s), skip its `depends_on` chain | Restart one service without disturbing the rest, e.g. `docker compose up -d --no-deps agent` after an env var change — see [Common operations](#common-operations) for concrete examples (DB password rotation, model switch, re-running ingest). |
| `--force-recreate [service]` | Recreate the container even if its image/config is unchanged | `ingest` is a one-shot: once it's exited 0, plain `docker compose up` leaves it alone and won't rerun it. Use `docker compose up -d --force-recreate ingest` (or `docker compose rm -f ingest` first) to force a fresh ingest pass. |
| `--wait` | Block until every started container reports healthy (or fails), then return | Useful in scripts, or when you just want a clear "stack is actually ready" signal instead of polling `docker compose ps` yourself: `docker compose up --build -d --wait`. |
| `--remove-orphans` | Remove containers for services no longer present in `docker-compose.yaml` | Only relevant after renaming/deleting a service in the compose file — not needed for routine use. |
| `--pull always` | Re-pull an image even if a local copy with the same tag already exists | Not needed here — every image in this stack is pinned to a specific version tag (e.g. `pgvector/pgvector:pg16`, `jaegertracing/all-in-one:1.60`), not `latest`, so a cached pull is always the right one. |

Not a flag, but related: **`litellm` is consistently the slowest service to report healthy** (up to ~115s worst case — `start_period: 15s` + `interval: 10s` × `retries: 10` in its healthcheck). This is inherent to the image (full Python app, provider-credential validation at boot), not a misconfiguration — `agent` and `ingest` both wait on it (`depends_on: litellm: condition: service_healthy`), so it's usually the visible bottleneck in `docker compose ps` right after `up`.

## Verifying everything is healthy

```bash
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
```
`postgres`, `litellm`, and `agent` should read `Up ... (healthy)` — those three are the only ones with a `healthcheck:` defined. Everything else (`jaeger`, `otel-collector`, `prometheus`, `blackbox-exporter`, `loki`, `alloy`, `grafana`) will just show `Up ...` with no health suffix — that's normal, not a sign anything's wrong, they just don't have Docker-level healthchecks configured (their actual health is what `blackbox-exporter`'s probes and the "A service is down" alert check instead, see [Alerts](#alerts)). `ingest` should show `Exited (0)`.

Spot checks:
```bash
curl -s localhost:8000/health          # {"status":"ok"}  — agent + its DB connection
curl -s localhost:4000/health/liveliness    # litellm gateway
docker compose exec postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```
(`$POSTGRES_USER` is set inside the containers, not your host shell — wrap in `sh -c '...'` so it expands there, unless you've `source .env`d it locally.)

If `ingest` is still `Exited (1)` or missing, check its logs — see [Troubleshooting](#troubleshooting).

## Running a task

Use `scripts/run_task.sh` — it calls the already-running `agent` service directly over HTTP (`curl`), so it's instant and never spins up a new container or re-triggers ingest, unlike `docker compose run`. Requires `curl` and `jq` on the host (both already present here).

```bash
./scripts/run_task.sh "<task>" [date_from] [date_to] [model]
```

`date_from`, `date_to`, and `model` are positional and all optional — pass empty strings (`""`) to skip one while still setting a later one, e.g.:
```bash
./scripts/run_task.sh "Summarize news about oil and airlines in early 2015, and relate that to how AAL traded that period" 2015-01-01 2015-03-31
./scripts/run_task.sh "What was AAPL's most recent reported net income?"                    # no dates
./scripts/run_task.sh "Compare AAPL and MSFT margins" "" "" gpt-agent                        # skip dates, override model
```

Output includes `trace_id`, tokens (`prompt_tokens`/`completion_tokens`/`total_tokens`), and `cost_usd` (real dollar cost, from litellm) for that specific call — and the script prints a direct Jaeger trace link to stderr.

By default it targets `http://localhost:8000`; override with `AGENT_URL` to call it from another machine on the network:
```bash
AGENT_URL=http://dev01.lab.home.arpa:8000 ./scripts/run_task.sh "..."
```

**Containerized alternative** (`agent/run_task.py`, invoked via `docker compose run --rm agent python run_task.py --task "..." --date-from ... --date-to ... --model ...`) — kept around for environments without `curl`/`jq` on the host, or when you don't want port 8000 exposed at all. Functionally equivalent, just pays ~1-2s of container-startup overhead per call.

### Sample tasks to try

Data coverage: `prices` spans 2010-01-04 to 2016-12-30, `fundamentals` spans 2003-06-30 to 2017-01-01 (quarterly/annual periods), `news_headlines` spans 2003-02-19 to 2021-12-31. Tickers confirmed present in both `fundamentals` and `prices`: `AAPL`, `MSFT`, `AAL`, `XOM`, `JPM` (and 440+ others — `SELECT DISTINCT ticker FROM fundamentals` for the full list).

```bash
# Fundamentals only, undated — most recent reported period
./scripts/run_task.sh "What was AAPL's most recent reported net income and profit margin?"

# Fundamentals trend across periods
./scripts/run_task.sh "Compare MSFT's revenue and profit margin across its last four reported periods. Is the trend improving?"

# Price history, dated
./scripts/run_task.sh "How did XOM stock perform in Q4 2015? Mention the high, low, and overall trend." 2015-10-01 2015-12-31

# News full-text search only, dated (no ticker involved)
./scripts/run_task.sh "What major headlines were reported around the September 2008 financial crisis?" 2008-09-01 2008-10-31

# Combined: news + price history, dated (the "full stack" demo)
./scripts/run_task.sh "Summarize news about oil and airlines in early 2015, and relate that to how AAL traded that period" 2015-01-01 2015-03-31

# Cross-ticker comparison (multiple tool calls, multiple tickers)
./scripts/run_task.sh "Compare AAPL and MSFT: which had stronger revenue growth between 2013 and 2015?"

# Deliberate miss, to see tool-error handling
./scripts/run_task.sh "What was TSLA's net income in 2015?"   # TSLA isn't in fundamentals — watch it get a clear tool error, not a crash
```

Equivalent direct `curl`, useful for scripting or a quick manual check without the wrapper script:
```bash
curl -s -X POST localhost:8000/run -H 'content-type: application/json' \
  -d '{"task": "...", "date_from": "2015-01-01", "date_to": "2015-03-31"}' | python3 -m json.tool
```

## Accessing the UIs

| UI | URL | Notes |
|---|---|---|
| Jaeger (traces) | http://localhost:16686 | Search by service `agent`, or paste a `trace_id` from a task response |
| Grafana (dashboards) | http://localhost:3000 | Anonymous access enabled (Admin role) for this local setup; "Agent Platform Overview" dashboard is pre-provisioned |
| Prometheus (raw metrics) | http://localhost:9090 | Query e.g. `rate(agent_tool_calls_total[5m])` |
| Loki (raw logs) | http://localhost:3100 | No real UI — query via its HTTP API directly, or (preferred) through Grafana Explore, see [Viewing logs](#viewing-logs) |
| Alloy (log shipper) | http://localhost:12346 | Its own web UI showing the discovery/pipeline graph — useful for confirming it's actually finding and tailing every container |
| litellm | http://localhost:4000 | Gateway; not meant for interactive use, but useful for `curl` debugging |

## Dashboard panels

The "Agent Platform Overview" dashboard (Grafana → Dashboards, or http://localhost:3000/d/agent-overview) has 9 panels, sourced from `observability/grafana/dashboards/json/agent-overview.json`. `model` in any of these is the **resolved** provider/model (e.g. `anthropic/claude-sonnet-4-5-20250929`, from litellm's `x-litellm-model-name` response header) — not the `AGENT_MODEL` routing alias (e.g. `claude-agent`).

| # | Panel | Query | What it tells you |
|---|---|---|---|
| 1 | Task runs / min | `sum(rate(agent_tasks_total[5m])) by (status)` | Throughput, split `ok` vs `failed` (e.g. hit `MAX_ITERATIONS`). A rise in `failed` is the first thing to alert on. |
| 2 | Task duration (p50/p95) | `histogram_quantile(...agent_task_duration_seconds_bucket...)` | End-to-end latency per task. p95 diverging from p50 means a tail of much-slower tasks (usually ones needing many tool-call iterations). |
| 3 | LLM calls by status | `sum(rate(agent_llm_calls_total[5m])) by (model, status)` | Rate of individual LLM calls (a task can make several — see [Running a task](#running-a-task) on iteration count), by resolved model and `ok`/`error`. |
| 4 | Tool calls by tool/status | `sum(rate(agent_tool_calls_total[5m])) by (tool, status)` | Same, per tool. `search_news_semantic` showing steady `tool_error` is expected until the embedding subset is populated (see Troubleshooting). |
| 5 | Token usage rate (prompt vs completion) | `sum(rate(agent_llm_tokens_total[5m])) by (model, type)` | Tokens/sec, split prompt vs completion, per resolved model. |
| 6 | Tokens per task (p50/p95) | `histogram_quantile(...agent_task_tokens_total_bucket...)` | Distribution of total tokens per completed task — typical size vs. a long tail of expensive ones. |
| 7 | Total tokens used | `sum(agent_llm_tokens_total)` | Running total. **Cumulative since the `agent` container last started** — resets on restart/rebuild (in-memory counter), even though Prometheus's own history (panels 1-6, 9) survives that. |
| 8 | Total cost (USD) | `sum(agent_llm_cost_usd_total)` | Same idea in real dollars — litellm's actual billed cost per call, not a token-count estimate. Same restart caveat as #7. |
| 9 | Cost by model ($/5m) | `sum(rate(agent_llm_cost_usd_total[5m])) by (model)` | Spend rate over time, per resolved model — useful once routing across multiple models/providers. |

**If you see two series for what should be one model** (e.g. an old alias like `claude-agent` alongside the real `anthropic/...` name): that's stale history from before a labeling change, not a second model actually running — Prometheus starts a new time series whenever a label *value* changes, it doesn't rewrite old samples. The stale series has no new data coming in and will age out of any rolling time window on its own; no action needed unless you want to wipe Prometheus's `promdata` volume for a clean slate (which also discards everything else's history).

### Agent Custom Metrics (raw)

A second dashboard (`observability/grafana/dashboards/json/agent-custom-metrics.json`, uid `agent-custom-metrics`) — one panel per custom `agent_*` metric, nothing curated or aggregated beyond a simple rate/average. Exists because Grafana's own **Metrics** auto-discovery view (Explore → Metrics) mixes our 10 custom metrics in with the ~10 `python_*`/`process_*` metrics `prometheus_client` registers automatically (and expands further per label-combination/histogram-bucket, so it shows 50+ panels there, not 10) — this dashboard is the "just the 10 things we actually instrument, nothing else" view. See the earlier chat-style breakdown of which of the 30 raw `/metrics` HELP lines are custom vs. freebie if you want the full accounting; the short version is 10 metrics we defined in `agent/observability.py`, each expanding into 2 (`Counter`s: `_total`+`_created`) or 4 (`Histogram`s: `_bucket`+`_sum`+`_count`+`_created`) raw series.

Histogram panels here show a rolling **average** (`sum(rate(x_sum[5m])) / sum(rate(x_count[5m]))`) rather than percentiles — the curated dashboard above already has p50/p95 for task duration and tokens; this one prioritizes "one simple number per metric" over duplicating that.

## Viewing logs

Every container's stdout is captured — `alloy` discovers all of them via the Docker API (no per-service config needed; add a new service to `docker-compose.yaml` and its logs start flowing automatically) and ships them to `loki`.

**In Grafana** (preferred): left sidebar → **Explore** → switch the datasource dropdown to **Loki**.

`service` matches the `docker-compose.yaml` service name (`agent`, `postgres`, `litellm`, ...); there's also a `container` label with the full container name (`agent-platform-agent-1`) if you need to disambiguate after a rebuild. `| json` only works on `{service="agent"}` — it's the only service emitting structured JSON logs (`agent/observability.py`'s `JsonFormatter`); every other service logs plain text, so piping those through `| json` returns nothing.

**Basic filters:**
```logql
{service="agent"}                          # everything from the agent
{service="postgres"}                        # any other service — same pattern
{service=~"agent|litellm"}                  # multiple services (regex alternation)
{container="agent-platform-agent-1"}        # by full container name instead of service
```

**Filtering on JSON fields** (agent only):
```logql
{service="agent"} | json | level="ERROR"
{service="agent"} | json | level="WARNING"
{service="agent"} | json | logger="agent_loop"
{service="agent"} | json | trace_id="<paste-a-trace-id>"     # everything from one specific task run
```

**Text search** (works on any service, JSON or not):
```logql
{service="agent"} |= "task completed"
{service="agent"} |= "tool_error"
{service="litellm"} |= "error"
{service="agent"} != "GET /health"          # exclude noisy health-check lines
```

**The specific patterns this codebase actually logs** (from `agent_loop.py`) — these are what the alert rules' `logs_link` annotations use under the hood, see [Alerts](#alerts):
```logql
{service="agent"} | json | msg=~"task started.*"
{service="agent"} | json | msg=~"task completed.*"
{service="agent"} | json | msg=~"task exceeded token threshold.*"
{service="agent"} | json | msg=~"task exceeded cost threshold.*"
{service="agent"} | json | msg=~"tool_error tool=search_news_semantic.*"
{service="agent"} | json | msg=~"tool_error tool=search_news_fulltext.*"
{service="agent"} | json | msg=~"slow llm call.*"
{service="agent"} | json | msg=~"llm call failed.*"
```

**Narrow to one tool or ticker:**
```logql
{service="agent"} | json | msg=~"tool_error tool=get_price_history.*"
{service="agent"} |= "AAPL"                 # crude but works — searches raw text
```

**Metric queries** (turn logs into a rate/count — renders as a graph, not a log list):
```logql
sum(rate({service="agent"} |= "tool_error" [5m]))                       # tool_error lines/sec
sum(count_over_time({service="agent"} | json | level="ERROR" [1h]))     # error count, last hour
sum by (service) (rate({job="docker"}[5m]))                             # log volume per service
```

**Combine multiple conditions:**
```logql
{service="agent"} | json | level="WARNING" | msg=~".*token threshold.*"
{service="agent"} | json | trace_id="abc123" | level!="INFO"   # only non-INFO lines for one trace
```

**Time range**: the query box only holds the filter — set the actual window with Grafana's time picker (top-right), or via API `start`/`end` params (see below).

**Gotchas**: label matching (`service="agent"`) is exact-string; `msg=~"..."` after `| json` is regex, so a literal `.` needs escaping if you mean it literally, not "any character."

**Trace correlation, both directions**: the agent's JSON logs always carry a top-level `trace_id` field when one is available (see `agent/observability.py`'s `JsonFormatter`).
- **Log → trace**: in Grafana's log view (Explore → Loki), any line containing `"trace_id":"..."` gets an automatic **TraceID** link (a `derivedField` on the Loki datasource, pointing at Jaeger) — click it to jump straight from a log line to the exact trace that produced it.
- **Trace → log**: in Grafana's trace view (Explore → Jaeger, or from a trace opened via a log's TraceID link), click any span and use **"Logs for this span"** — a `tracesToLogsV2` entry on the Jaeger datasource runs `{service="agent"} | json | trace_id="<that span's trace ID>"` against Loki automatically.

Either way you land on the same correlated data — which direction you start from just depends on whether a metric spike led you to a trace first, or a `grep`-style log search found something odd first.

Both of these are Grafana datasource-provisioning config (`observability/grafana/datasources/datasources.yml`), not application code — if you ever edit that file, **you must restart Grafana** (`docker compose restart grafana`) to pick it up; datasource provisioning only runs at startup, unlike dashboard JSON which re-syncs on an interval. One gotcha to know about if you touch it: any `${...}` template variable in that YAML (e.g. `${__trace.traceId}`, `${__value.raw}`) needs to be written as `$${...}` (doubled `$`) — Grafana's provisioning loader does its own env-var substitution pass over the file before storing the config, and a single `$` gets silently swallowed as an attempt to substitute a (nonexistent) environment variable, leaving the placeholder missing entirely rather than erroring loudly.

**Directly via the API**, useful for scripting:
```bash
curl -s -G "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={service="agent"} |= "task completed"' \
  --data-urlencode "start=$(($(date +%s%N)-3600000000000))" \
  --data-urlencode "end=$(date +%s%N)" | python3 -m json.tool
```

Logs persist in the `lokidata` volume across container restarts, but are wiped by `docker compose down -v` like the other observability volumes (`promdata`, `grafanadata`) — same tradeoff as metrics/traces, see [Shutting down / resetting](#shutting-down--resetting).

## Alerts

Grafana → left sidebar → **Alerting → Alert rules** (folder: "Agent Platform"). Rules are provisioned from `observability/grafana/alerting/rules.yaml` — edit that file, then `docker compose restart grafana` to pick it up (same rule as datasource/dashboard provisioning: it only loads at startup).

| Alert | Fires when | Why this one |
|---|---|---|
| Semantic search failing | `search_news_semantic` returns `tool_error` in the last 5m | Directly surfaces the "no embeddings populated" condition instead of it being silently absorbed by the fulltext fallback |
| Task token usage exceeds threshold | Any single task used >10,000 tokens (last 10m) | Flags unusually expensive/looping tasks — see `agent_task_tokens_total` histogram |
| Task cost exceeds threshold | Any single task cost >$0.05 (last 10m) | Same idea in real dollars — `agent_task_cost_usd` histogram |
| A service is down | `probe_success == 0` for any blackbox-probed target | Fires **once per service**, named in the `instance` label — not one opaque "something's down" |
| Task failure rate high | >20% of tasks failed in the last 10m | Catches systemic issues (e.g. `MAX_ITERATIONS` becoming common) that the semantic-search alert wouldn't |
| LLM call errors | Any `agent_llm_calls_total{status="error"}` in the last 5m | Distinct from tool errors — this is the litellm/provider path specifically |
| LLM call latency high (p95) | p95 LLM call duration >15s over 10m | Leading indicator of provider degradation, before it starts causing timeouts/failures |

**Why token/cost thresholds are histograms, not just counters**: a `Counter` can tell you total spend, but can't answer "did any *single* task exceed $X" — only a distribution (`Histogram`) can. Both alerts work by subtracting the `+Inf` bucket's count from the threshold bucket's count (`sum(increase(..._bucket{le="+Inf"}[10m])) - sum(increase(..._bucket{le="<threshold>"}[10m]))`), which gives "how many observations landed strictly above the threshold." **This only works if the threshold is an exact existing bucket boundary** (see the `buckets=(...)` tuples in `agent/observability.py`) — if you change a threshold, add a matching bucket boundary too, or the subtraction silently returns nothing useful.

**Gotcha**: Prometheus's `le` bucket label is a *string*, and whole-number bucket boundaries get formatted with a trailing `.0` (`le="10000.0"`, not `le="10000"`) while fractional ones don't (`le="0.05"` is exactly that). Label matching is exact-string, so getting this wrong makes the query silently return no data rather than erroring — always verify against the raw metric (`agent_task_tokens_total_bucket` / `agent_task_cost_usd_bucket` in Prometheus) before trusting a bucket-based alert query.

**Alerts don't carry task-level context on their own** — a Prometheus alert only knows an aggregate number crossed a threshold, not *which* task/prompt/call caused it. To close that gap, `agent_loop.py` logs a structured `WARNING` line with the actual task text and `trace_id` at the exact moment a threshold is crossed (token/cost thresholds, tool errors, slow LLM calls). Every rule except "A service is down" (see below) has a `logs_link` annotation — a pre-built Grafana Explore URL with the right Loki query already filled in. Open the rule (Alerting → Alert rules → click it), and the annotation renders as a clickable link straight to the matching logs; no copy-pasting LogQL by hand.

**This link is hardcoded to `http://localhost:3000/...` and that's a known, deliberate limitation, not an oversight** — two other approaches were tried and empirically ruled out first:
- A relative URL (`/explore?...`, no host) — Grafana's annotation renderer does **not** auto-linkify it; it just shows as plain unclickable text.
- Making the host configurable via a `${GRAFANA_EXTERNAL_URL}` env var substituted into the provisioning YAML — confirmed not to work by testing directly (set a throwaway annotation referencing a real, definitely-set env var, restarted, it came back completely unsubstituted). Alert-rule provisioning's Go-template annotations only ever have access to `$labels`/`$values` — no server config, no request context, nothing that could resolve "whatever host you're currently viewing this from." Datasource provisioning *does* support `${VAR}` substitution (used elsewhere in this repo, see `datasources.yml`); alert-rule provisioning simply doesn't, and there's no workaround for that within Grafana's own annotation system.

So: this link works as a clickable shortcut if you're browsing Grafana at literally `localhost:3000` (e.g. on the same machine, or via an SSH tunnel/port-forward to it). If you're on a different hostname (like `dev01.lab.home.arpa:3000`), it'll open with the wrong host — either fix the host in the address bar after clicking, or skip the link entirely and copy the LogQL out of its `expr` query parameter to run manually per [Viewing logs](#viewing-logs) — that path is host-agnostic by construction. For the token-threshold alert, that's:
```logql
{service="agent"} | json | msg=~"task exceeded token threshold.*"
```

**Pending vs. firing**: every rule has `for: 1m` (2m for the noisier ones) — a condition has to stay true for that whole window before the alert moves from `Pending` to `Firing`. This avoids flapping on a single noisy data point; it also means there's a real ~1-2 minute delay between "the bad thing happened" and "the alert shows as firing," which is expected, not a bug.

**`{{ $labels.instance }}` shows up literally, unrendered, in the rule list** — that's the raw annotation template, and it's normal; Grafana only substitutes real values (e.g. `postgres:5432 failed its health probe`) once you're looking at a specific *firing instance* of the rule (click into the rule → expand an instance), not the rule definition itself.

**No notification channel is configured** — these alerts are visible in Grafana's UI (and via the `/api/prometheus/grafana/api/v1/rules` / `/api/alertmanager/grafana/api/v2/alerts` APIs) but won't page/email/Slack anyone. That's a deliberate scope cut for local dev — see `DECISIONS.md` for what a real contact point/notification policy would add for production.

## Common operations

### Change the agent's DB password
`POSTGRES_AGENT_RO_PASSWORD` in `.env` — can be any string, doesn't need to match anything external. Unlike `POSTGRES_PASSWORD` (only applied by Postgres on a genuinely fresh volume), this one is re-synced via `ALTER ROLE` on every `ingest` run, so:
```bash
# edit POSTGRES_AGENT_RO_PASSWORD in .env, then:
docker compose up -d --no-deps ingest agent
```
takes effect immediately — no volume wipe needed. (`ingest` re-applies `sql/schema.sql`, which sets the password unconditionally; `agent` needs recreating too so it picks up the new value in its own connection string.)

### Re-run ingest without restarting the whole stack
```bash
docker compose up -d --no-deps ingest
```
No-ops instantly if tables are already populated.

### Force a fresh raw parse (ignore any existing export)
```bash
DATA_SOURCE=raw docker compose run --rm ingest
```
Only makes sense against an *empty* database — if tables already have rows, ingest skips regardless of `DATA_SOURCE`. To actually reload, wipe first (see [Shutting down / resetting](#shutting-down--resetting)).

### Embed the full 1.24M headline corpus (not done by default)
Run standalone, **before** the normal `docker compose up`:
```bash
NEWS_EMBED_LIMIT=1244184 DATA_SOURCE=raw EXPORT_OVERWRITE=true \
    docker compose run --rm ingest
```
This calls OpenAI ~12,500 times (batches of 100) — expect real cost and a long run (rate-limit dependent). It writes a fully-embedded `data/export/news_headlines.copy.gz`; every `docker compose up` after that restores it automatically (`DATA_SOURCE=auto`, the default) with **no re-embedding and no further OpenAI calls** — the embedding vectors are just another column, and standard Postgres `COPY` (what the export/import uses) handles pgvector's `vector` type transparently, no special-casing needed.

The pgvector ANN index (`idx_news_embedding`) does **not** travel via that export the same way — it's DDL, not data, and no data-export mechanism (`COPY`, `pg_dump --data-only`, etc.) ever includes an index. `ingest.py`'s `ensure_ann_index()` handles this by checking "are there embedded rows and does the index not exist yet" on *every* run, independent of whether this run did the embedding or restored it from export — so the index still gets built correctly on a fresh `DATA_SOURCE=auto` restore, not just on the original embedding run.

### Quick small-scale dev iteration (don't touch the real export)
```bash
DATA_SOURCE=raw NEWS_ROWS_LIMIT=1000 docker compose run --rm ingest
```
Loads only 1,000 headline rows into a fresh DB for a fast sanity check. `EXPORT_OVERWRITE` defaults to `false`, so this **will not** clobber a bigger/better export already on disk — you'll see a `WARNING ... leaving it as-is` log line confirming that.

### Switch models
Edit `AGENT_MODEL` in `.env` (must match a `model_name` in `litellm/config.yaml`), or pass `--model` per-request to `run_task.py`. To add a new provider/model, add an entry to `litellm/config.yaml` and restart `litellm`:
```bash
docker compose up -d --no-deps litellm
```

### View logs
```bash
docker compose logs -f agent           # task activity, structured JSON lines
docker compose logs -f ingest          # ingestion progress
docker compose logs -f litellm         # provider call failures, rate limits
```

### Check current data volumes
```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM fundamentals"'
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM prices"'
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM news_headlines"'
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM news_headlines WHERE embedding IS NOT NULL"'
```

## Troubleshooting

**Dashboards/datasources aren't picking up a provisioning config change (e.g. moved to a different folder) even after `docker compose restart grafana`**
Grafana's file-based provisioner only applies certain settings — like which folder a dashboard lives in — at the moment it *first* creates that object. Changing `observability/grafana/dashboards/dashboards.yml`'s `folder:` (or similar) afterward and restarting does not retroactively move already-provisioned dashboards; Grafana also blocks deleting a provisioned dashboard/folder via the UI or API (`"provisioned dashboard cannot be deleted"`) specifically so it can't drift from the file. The actual fix is to force a fresh reprovision:
```bash
docker compose stop grafana
docker compose rm -f grafana
docker volume rm agent-platform_grafanadata
docker compose up -d grafana
```
This wipes Grafana's own state (dashboard star/view history, anonymous session, etc. — nothing that matters here) and rebuilds everything from the committed YAML/JSON, which is the actual desired state anyway. Same root cause as the `$${...}` env-var-substitution gotcha under [Alerts](#alerts) — Grafana's provisioning system generally treats "already provisioned" objects as something to reconcile *forward* from, not fully re-derive from a changed config file.

**`ingest` exits 1 immediately with `DATA_SOURCE=export but no export file found`**
You forced `DATA_SOURCE=export` before any raw run ever produced `data/export/*.copy.gz`. Either unset `DATA_SOURCE` (defaults to `auto`, which falls back to raw automatically) or run once with `DATA_SOURCE=raw`.

**`ingest` logs long strings of `429 Too Many Requests` on `/embeddings`, then a `WARNING ... skipping semantic embedding entirely`**
Expected and handled — either real rate limiting or (check the underlying error in `docker compose logs litellm`) an OpenAI key with no billing/quota (`insufficient_quota`). Ingest still completes successfully; `news_headlines` is fully loaded and full-text searchable, just not semantically embedded. `search_news_semantic` will return a clear tool error telling the agent to use `search_news_fulltext` instead — this is not a broken run.

**A task run returns `502` with `exceeded MAX_ITERATIONS=6 without a final answer`**
The model used all 6 tool-calling round trips without producing a final answer — usually a sign it kept retrying a failing tool. Open the `trace_id` from the error message in Jaeger and look at which tool kept erroring. If it's `search_news_semantic` failing repeatedly, confirm embeddings actually exist (`SELECT count(*) FROM news_headlines WHERE embedding IS NOT NULL` — see above); if zero, that's expected (see previous entry) and the model should have fallen back to `search_news_fulltext` — if it didn't, it's worth widening `MAX_ITERATIONS` in `agent/agent_loop.py` or tightening the system prompt.

**`agent` container stuck at `health: starting` / never becomes healthy**
```bash
docker compose logs agent
```
Usually means it can't reach `postgres` or `litellm` yet, or the `agent_ro` role/grants from `sql/schema.sql` weren't applied (check `ingest` succeeded first — `agent` won't even start until `ingest` reports `service_completed_successfully`).

**Port already in use (`5432`, `8000`, `3000`, `9090`, `9115`, `16686`, `4000`, `3100`, `12346`)**
Something else on the host is bound to that port. Either stop it, or remap the host side in `docker-compose.yaml` (left side of `"HOST:CONTAINER"` under the relevant service's `ports:`). Hit exactly this with `alloy`'s default port `12345` on a host that already had a native Grafana Alloy agent running system-wide — that's why the compose file maps it to `12346:12345` instead; if your host's `12346` is also taken, remap again.

**No logs showing up for a service in Grafana/Loki**
Check `alloy`'s own logs (`docker compose logs alloy`) for `final error sending batch` — usually means it can't reach `loki:3100` (wrong URL — the Loki push endpoint is `/loki/api/v1/push`, easy to get wrong) or Loki itself isn't healthy yet. Also confirm `alloy` actually has the Docker socket mounted (`/var/run/docker.sock`) — without it, `discovery.docker` finds nothing and there's nothing to ship, silently.

**`docker compose` command not found**
This host needs the Compose v2 plugin: `sudo apt-get install -y docker-compose-v2` (or your distro's equivalent), then re-run.

**Postgres data looks stale / wrong after changing `sql/schema.sql`**
Schema changes only apply to a fresh database — `ingest` runs `schema.sql` every time but most statements are `CREATE ... IF NOT EXISTS`, so column changes on existing tables won't retroactively apply. Wipe and re-ingest (see below).

## Shutting down / resetting

```bash
docker compose down            # stop everything, keep data volumes + export/ dir
docker compose down -v         # also delete postgres/prometheus/grafana/loki volumes (full reset)
```

`data/export/*.copy.gz` is **not** touched by either command (it's a bind mount, not a named volume) — after `down -v`, the next `docker compose up` will restore from it in under a minute rather than re-parsing CSVs. To force a genuinely from-scratch ingest (e.g. after a raw CSV or schema change), also delete the export files:
```bash
rm -f data/export/*.gz
docker compose down -v
docker compose up --build -d
```
