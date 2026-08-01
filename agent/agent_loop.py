"""
The tool-calling loop: task -> LLM (via litellm) -> tool calls -> LLM -> ...
-> final answer.

Safety guardrails:
  - MAX_ITERATIONS bounds how many LLM<->tool round trips a single task can
    take, so a confused model can't loop forever or run up cost.
  - TOOL_TIMEOUT_SECONDS bounds each individual tool call.
  - Tool errors are fed back to the model as a normal message (so it can
    recover, e.g. retry with a different ticker) rather than crashing the run.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from opentelemetry import trace

from observability import (
    LLM_CALL_COUNTER,
    LLM_CALL_LATENCY,
    LLM_COST_COUNTER,
    LLM_TOKENS_COUNTER,
    TASK_COST,
    TASK_TOKENS,
    TOOL_CALL_COUNTER,
    TOOL_CALL_LATENCY,
)
from tools import TOOL_SCHEMAS, Tools, ToolError

log = logging.getLogger("agent_loop")
tracer = trace.get_tracer("agent_loop")

MAX_ITERATIONS = 6
TOOL_TIMEOUT_SECONDS = 20
# Mirror the bucket boundaries the Grafana alert rules key off of (see
# observability/grafana/alerting/rules.yaml) — kept as a second copy rather
# than a shared source of truth for now (the alert rules are Grafana-side
# YAML, this is Python), so if you change one, change the other. When these
# are crossed we log a WARNING with the actual task text and trace_id, since
# the Prometheus-based alert itself only sees an aggregate number and cannot
# say *which* task caused it — this log line is how you find out.
TASK_TOKENS_ALERT_THRESHOLD = 10000
TASK_COST_ALERT_THRESHOLD_USD = 0.05
LLM_CALL_LATENCY_WARN_SECONDS = 10
# Span attributes aren't meant to hold large blobs, and in a real deployment
# raw prompts (here: financial data / headlines, but in general potentially
# sensitive) sitting in full in a tracing backend indefinitely is its own
# decision to make deliberately, not by accident — so this is a deliberately
# truncated debug aid, not a full prompt log.
PROMPT_SPAN_ATTR_MAX_CHARS = 4000

_tool_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool-call")

SYSTEM_PROMPT = """You are a financial research assistant with access to tools over three \
datasets: company fundamentals (financial statements), daily stock prices, and a large news \
headline archive. Use the tools to ground your answer in actual data rather than speculating. \
If a date range is given for the task, scope your news and price lookups to it. When you're \
unsure whether a ticker or headline exists, call the tool and read the error rather than \
guessing. Cite the specific figures/headlines you used in your final answer."""


class TaskFailed(Exception):
    def __init__(self, message: str, trace_id: str | None = None):
        super().__init__(message)
        self.trace_id = trace_id


def run_task(client, model: str, tools: Tools, task: str, date_from: str | None, date_to: str | None) -> dict:
    user_content = task
    if date_from or date_to:
        user_content += f"\n\n(Scope date range: {date_from or 'earliest'} to {date_to or 'latest'})"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    tool_impls = {
        "get_fundamentals": tools.get_fundamentals,
        "get_price_history": tools.get_price_history,
        "search_news_semantic": tools.search_news_semantic,
        "search_news_fulltext": tools.search_news_fulltext,
    }

    trace_log: list[dict] = []

    with tracer.start_as_current_span("agent.run_task") as root_span:
        root_span.set_attribute("agent.task", task[:200])
        root_span.set_attribute("agent.model", model)
        otel_trace_id = format(root_span.get_span_context().trace_id, "032x")
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cost_usd = 0.0

        for iteration in range(1, MAX_ITERATIONS + 1):
            with tracer.start_as_current_span("agent.llm_call") as span:
                span.set_attribute("agent.iteration", iteration)
                span.set_attribute(
                    "agent.llm.prompt",
                    json.dumps(messages, default=str)[:PROMPT_SPAN_ATTR_MAX_CHARS],
                )
                start = time.monotonic()
                try:
                    raw_response = client.chat.completions.with_raw_response.create(
                        model=model,
                        messages=messages,
                        tools=TOOL_SCHEMAS,
                        tool_choice="auto",
                        timeout=60,
                    )
                except Exception as e:
                    LLM_CALL_COUNTER.labels(model=model, status="error").inc()
                    log.error("llm call failed: %s", e, extra={"trace_id": otel_trace_id})
                    raise TaskFailed(f"LLM call failed: {e}", trace_id=otel_trace_id) from e

                # litellm resolves our AGENT_MODEL alias (e.g. "claude-agent") to a
                # real provider/model (e.g. "anthropic/claude-sonnet-4-5-20250929")
                # and reports it — and the actual USD cost it billed — via response
                # headers. That's what we key metrics/spans by, not the alias.
                actual_model = raw_response.headers.get("x-litellm-model-name", model)
                cost_header = raw_response.headers.get("x-litellm-response-cost")
                response = raw_response.parse()

                call_duration = time.monotonic() - start
                LLM_CALL_LATENCY.labels(model=actual_model).observe(call_duration)
                LLM_CALL_COUNTER.labels(model=actual_model, status="ok").inc()
                span.set_attribute("agent.llm.requested_model", model)
                span.set_attribute("agent.llm.actual_model", actual_model)

                if call_duration > LLM_CALL_LATENCY_WARN_SECONDS:
                    log.warning(
                        "slow llm call: model=%s iteration=%d duration=%.1fs task=%r",
                        actual_model, iteration, call_duration, task[:200],
                        extra={"trace_id": otel_trace_id},
                    )

                if cost_header is not None:
                    cost = float(cost_header)
                    LLM_COST_COUNTER.labels(model=actual_model).inc(cost)
                    total_cost_usd += cost
                    span.set_attribute("agent.llm.cost_usd", cost)

                usage = response.usage
                if usage is not None:
                    LLM_TOKENS_COUNTER.labels(model=actual_model, type="prompt").inc(usage.prompt_tokens)
                    LLM_TOKENS_COUNTER.labels(model=actual_model, type="completion").inc(usage.completion_tokens)
                    total_prompt_tokens += usage.prompt_tokens
                    total_completion_tokens += usage.completion_tokens
                    span.set_attribute("agent.llm.prompt_tokens", usage.prompt_tokens)
                    span.set_attribute("agent.llm.completion_tokens", usage.completion_tokens)

                choice = response.choices[0]
                msg = choice.message
                span.set_attribute(
                    "agent.llm.response",
                    json.dumps(msg.model_dump(exclude_none=True), default=str)[:PROMPT_SPAN_ATTR_MAX_CHARS],
                )

            messages.append(msg.model_dump(exclude_none=True))

            if not msg.tool_calls:
                trace_log.append({"iteration": iteration, "type": "final_answer"})
                total_tokens = total_prompt_tokens + total_completion_tokens
                TASK_TOKENS.observe(total_tokens)
                TASK_COST.observe(total_cost_usd)
                if total_tokens > TASK_TOKENS_ALERT_THRESHOLD:
                    log.warning(
                        "task exceeded token threshold: tokens=%d threshold=%d task=%r",
                        total_tokens, TASK_TOKENS_ALERT_THRESHOLD, task[:200],
                        extra={"trace_id": otel_trace_id},
                    )
                if total_cost_usd > TASK_COST_ALERT_THRESHOLD_USD:
                    log.warning(
                        "task exceeded cost threshold: cost_usd=%.6f threshold=%.2f task=%r",
                        total_cost_usd, TASK_COST_ALERT_THRESHOLD_USD, task[:200],
                        extra={"trace_id": otel_trace_id},
                    )
                root_span.set_attribute("agent.total_prompt_tokens", total_prompt_tokens)
                root_span.set_attribute("agent.total_completion_tokens", total_completion_tokens)
                root_span.set_attribute("agent.total_cost_usd", total_cost_usd)
                return {
                    "answer": msg.content,
                    "iterations": iteration,
                    "trace": trace_log,
                    "otel_trace_id": otel_trace_id,
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                    "total_tokens": total_tokens,
                    "cost_usd": round(total_cost_usd, 6),
                }

            for tool_call in msg.tool_calls:
                name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                with tracer.start_as_current_span(f"agent.tool.{name}") as tspan:
                    tspan.set_attribute("agent.tool.args", json.dumps(args)[:500])
                    start = time.monotonic()
                    impl = tool_impls.get(name)
                    try:
                        if impl is None:
                            raise ToolError(f"unknown tool {name!r}")
                        future = _tool_executor.submit(lambda: impl(**args))
                        result = future.result(timeout=TOOL_TIMEOUT_SECONDS)
                        status = "ok"
                        content = json.dumps(result, default=str)
                    except ToolError as e:
                        status = "tool_error"
                        content = json.dumps({"error": str(e)})
                        log.warning(
                            "tool_error tool=%s args=%s error=%s task=%r",
                            name, json.dumps(args), e, task[:200],
                            extra={"trace_id": otel_trace_id},
                        )
                    except FutureTimeoutError:
                        status = "timeout"
                        content = json.dumps({"error": f"tool {name} timed out after {TOOL_TIMEOUT_SECONDS}s"})
                        log.warning(
                            "tool_timeout tool=%s args=%s task=%r",
                            name, json.dumps(args), task[:200],
                            extra={"trace_id": otel_trace_id},
                        )
                    except Exception as e:
                        status = "error"
                        content = json.dumps({"error": f"internal error: {e}"})
                        log.exception(
                            "tool %s failed args=%s task=%r", name, json.dumps(args), task[:200],
                            extra={"trace_id": otel_trace_id},
                        )
                    elapsed = time.monotonic() - start
                    TOOL_CALL_LATENCY.labels(tool=name).observe(elapsed)
                    TOOL_CALL_COUNTER.labels(tool=name, status=status).inc()
                    tspan.set_attribute("agent.tool.status", status)
                    tspan.set_attribute("agent.tool.duration_s", elapsed)

                trace_log.append({"iteration": iteration, "type": "tool_call", "tool": name, "args": args, "status": status})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": content,
                    }
                )

        TASK_TOKENS.observe(total_prompt_tokens + total_completion_tokens)
        TASK_COST.observe(total_cost_usd)
        raise TaskFailed(
            f"exceeded MAX_ITERATIONS={MAX_ITERATIONS} without a final answer (trace_id={otel_trace_id}, "
            f"tokens used: {total_prompt_tokens} prompt + {total_completion_tokens} completion, "
            f"cost: ${total_cost_usd:.6f})",
            trace_id=otel_trace_id,
        )
