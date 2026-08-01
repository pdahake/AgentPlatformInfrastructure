import json
import logging
import os
import sys

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram

SERVICE_NAME = "agent"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Ambient OTel context (trace.get_current_span()) works correctly for
        # app.py's sync route handler — FastAPI/anyio's threadpool offload
        # copies contextvars into the worker thread — so it's a reliable
        # fallback here. It would NOT be reliable for a log call made from
        # *inside* agent_loop.py's own `_tool_executor` (a plain
        # concurrent.futures.ThreadPoolExecutor, which does not copy context
        # into submitted work) if one were ever added there. Preferring an
        # explicit `extra={"trace_id": ...}` when the caller already has the
        # value on hand is the more robust choice either way — it doesn't
        # depend on which thread pool happens to be involved.
        explicit_trace_id = getattr(record, "trace_id", None)
        if explicit_trace_id:
            payload["trace_id"] = explicit_trace_id
        else:
            span_context = trace.get_current_span().get_span_context()
            if span_context.is_valid:
                payload["trace_id"] = format(span_context.trace_id, "032x")
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def setup_tracing():
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    return trace.get_tracer(SERVICE_NAME)


# -- Prometheus metrics, scraped via /metrics --------------------------------

TASK_COUNTER = Counter("agent_tasks_total", "Completed agent task runs", ["status"])
TASK_LATENCY = Histogram("agent_task_duration_seconds", "End-to-end task duration")
# `model` here is the *resolved* provider/model (e.g. "anthropic/claude-sonnet-4-5-20250929",
# read from litellm's x-litellm-model-name response header) — not our internal
# AGENT_MODEL alias (e.g. "claude-agent"). The alias is a routing config knob;
# the resolved model is what actually generated the tokens and what you'd bill
# against, so that's what these metrics are keyed by. Falls back to the alias
# if the header is ever missing (e.g. talking to a non-litellm endpoint).
LLM_CALL_COUNTER = Counter("agent_llm_calls_total", "LLM calls made", ["model", "status"])
LLM_CALL_LATENCY = Histogram("agent_llm_call_duration_seconds", "LLM call duration", ["model"])
TOOL_CALL_COUNTER = Counter("agent_tool_calls_total", "Tool invocations", ["tool", "status"])
TOOL_CALL_LATENCY = Histogram("agent_tool_call_duration_seconds", "Tool call duration", ["tool"])

# Token usage — the number that actually maps to LLM spend, so it's tracked
# separately from call counts/latency. `type` is "prompt" or "completion"
# since they're usually priced differently.
LLM_TOKENS_COUNTER = Counter("agent_llm_tokens_total", "LLM tokens used", ["model", "type"])
TASK_TOKENS = Histogram(
    "agent_task_tokens_total",
    "Total tokens (prompt+completion) used per completed task",
    buckets=(500, 1000, 2500, 5000, 10000, 25000, 50000, 100000),
)

# Real USD cost, taken directly from litellm's x-litellm-response-cost header
# rather than computed from tokens * a hardcoded price table — litellm already
# tracks current per-model pricing, so this stays correct as pricing changes
# without us maintaining a duplicate price list.
LLM_COST_COUNTER = Counter("agent_llm_cost_usd_total", "Actual USD cost of LLM calls", ["model"])
# Per-task cost distribution (mirrors TASK_TOKENS) — the counter above is a
# running total, useful for spend-rate alerting; this histogram is what lets
# an alert ask "did any single task cost more than $X", which a counter alone
# cannot answer. 0.05 is an explicit bucket boundary — see the
# TaskCostExceedsThreshold alert rule, which depends on it being exact.
TASK_COST = Histogram(
    "agent_task_cost_usd",
    "Total USD cost per completed task",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
