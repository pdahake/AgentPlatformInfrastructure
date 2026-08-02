import json
import logging
import os
import sys

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from prometheus_client import Counter, Histogram
from strands.telemetry import StrandsTelemetry

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
        # *inside* tools.py's own `_tool_executor` (a plain
        # concurrent.futures.ThreadPoolExecutor, used for per-tool timeouts,
        # which does not copy context into submitted work) if one were ever
        # added there. Preferring an explicit `extra={"trace_id": ...}` when
        # the caller already has the value on hand is the more robust choice
        # either way — it doesn't depend on which thread pool happens to be
        # involved.
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
    # Strands' own StrandsTelemetry().setup_otlp_exporter() hardcodes the
    # HTTP OTLP exporter (opentelemetry.exporter.otlp.proto.http) with no way
    # to switch it to gRPC — but this stack's otel-collector only has a gRPC
    # receiver configured (observability/otel-collector-config.yaml, port
    # 4317, no HTTP receiver), and every other OTLP producer in this stack
    # already targets it over gRPC. Rather than add an HTTP receiver to
    # shared collector config just for this one service, build the
    # TracerProvider directly and hand it to StrandsTelemetry pre-built —
    # passing tracer_provider skips its
    # internal _initialize_tracer(), which is also what would normally set
    # the global tracer provider and W3C propagators, so both are done
    # explicitly here instead.
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", SERVICE_NAME)})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(
        CompositePropagator([W3CBaggagePropagator(), TraceContextTextMapPropagator()])
    )

    # Strands automatically emits per-cycle/per-LLM-call/per-tool-call spans
    # as children of whatever span is active when `agent(...)` is called
    # (see agent_loop.py's own root span) — using this same global provider,
    # since StrandsTelemetry(tracer_provider=provider) just stores it as-is
    # rather than creating a separate one.
    StrandsTelemetry(tracer_provider=provider)
    HTTPXClientInstrumentor().instrument()
    return trace.get_tracer(SERVICE_NAME)


# -- Prometheus metrics, scraped via /metrics --------------------------------

TASK_COUNTER = Counter("agent_tasks_total", "Completed agent task runs", ["status"])
TASK_LATENCY = Histogram("agent_task_duration_seconds", "End-to-end task duration")
# `model` here is the *resolved* provider/model (e.g. "anthropic/claude-sonnet-4-5-20250929",
# resolved from our AGENT_MODEL alias via agent_loop.py's local
# ALIAS_TO_MODEL map) — not the alias itself (e.g. "claude-agent"). The
# alias is a routing config knob; the resolved model is what actually
# generated the tokens and what you'd bill against, so that's what these
# metrics are keyed by.
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

# Real USD cost, computed via litellm.cost_per_token(actual_model, ...) —
# litellm's own client-side price table, not a hardcoded one we'd have to
# maintain ourselves, so this stays correct as pricing changes. Strands
# doesn't expose litellm proxy's raw HTTP response headers to application
# code, so this is computed client-side rather than read off a response
# header.
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
