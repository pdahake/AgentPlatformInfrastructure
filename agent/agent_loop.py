"""
The agent's task runner: task -> Strands `Agent` (LiteLLM proxy + tools) ->
final answer.

Strands owns the LLM<->tool round-trip loop, tool dispatch, and automatic
per-cycle/per-LLM-call/per-tool-call OTel spans (see observability.py's
setup_tracing()); this module is mostly about translating Strands' result/
metrics shape into this project's Prometheus metrics and threshold-crossing
alert logging.

Safety guardrails:
  - MAX_ITERATIONS bounds LLM<->tool round trips via Strands' `Limits(turns=...)`.
    A single LLM response requesting multiple tool calls still counts as one
    turn — Strands runs every tool call from one response within a single
    turn before the next LLM call.
  - Tool timeouts are enforced inside tools.py itself (per-@tool
    ThreadPoolExecutor wrapper), since Strands has no native per-call
    timeout — see tools.py's `_with_timeout`.
  - A raised exception inside a `@tool` function (including our own
    ToolError, or a timeout) is caught by Strands and fed back to the model
    as a normal tool-result message so it can recover — no manual
    try/except needed here for that case (verified empirically).
"""
from __future__ import annotations

import logging
import os

import litellm
from opentelemetry import trace
from strands import Agent
from strands.models.litellm import LiteLLMModel
from strands.types.agent import Limits

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
from tools import get_fundamentals, get_price_history, search_news_fulltext, search_news_semantic

log = logging.getLogger("agent_loop")
tracer = trace.get_tracer("agent_loop")

MAX_ITERATIONS = 6
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

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]

# Resolves our AGENT_MODEL alias (e.g. "claude-agent") to the real
# provider/model litellm's own config.yaml routes it to (e.g.
# "anthropic/claude-sonnet-4-5-20250929") — needed because
# litellm.cost_per_token() prices against the real model name, not our
# alias, and Strands' hook events don't expose litellm proxy's raw HTTP
# response headers (AfterModelCallEvent only exposes a normalized
# stop_reason/message).
#
# Deliberately a small LOCAL, KEY-FREE copy — NOT read from
# litellm/config.yaml itself, even though that file holds only
# `os.environ/VARNAME` placeholders rather than literal key values. Mounting
# litellm's actual routing config into this container would still violate
# this project's explicit isolation boundary (DECISIONS.md: "Provider keys
# isolated to the gateway container... reduces blast radius if the agent
# container [the thing processing untrusted task text] is compromised") —
# the agent has no business knowing which env vars would resolve to real
# keys, even via a placeholder-only file. A stale entry here only risks a
# wrong `cost_usd` metric, never a credential leak — keep it in sync with
# litellm/config.yaml's `model_list` by hand.
ALIAS_TO_MODEL = {
    "claude-agent": "anthropic/claude-sonnet-4-5-20250929",
    "gpt-agent": "openai/gpt-4o-mini",
}

SYSTEM_PROMPT = """You are a financial research assistant with access to tools over three \
datasets: company fundamentals (financial statements), daily stock prices, and a large news \
headline archive. Use the tools to ground your answer in actual data rather than speculating. \
If a date range is given for the task, scope your news and price lookups to it. When you're \
unsure whether a ticker or headline exists, call the tool and read the error rather than \
guessing. Cite the specific figures/headlines you used in your final answer.

If a tool returns an error or no data for what was asked (e.g. a ticker isn't in the \
fundamentals/price data, or a date range has no matching headlines), you must NOT supply a \
figure, date, or fact from your own general/training knowledge as a substitute — even with a \
caveat that it's unverified. State plainly that the data is not available in this system's \
tools, and stop there. A wrong-but-confident-sounding number is worse than no answer."""


class TaskFailed(Exception):
    def __init__(self, message: str, trace_id: str | None = None):
        super().__init__(message)
        self.trace_id = trace_id


def _build_agent(model_alias: str) -> Agent:
    # Routing goes entirely through the litellm proxy by alias
    # (model_id=model_alias, not the resolved real model) — the agent
    # container never sees ANTHROPIC_API_KEY/OPENAI_API_KEY directly, only
    # LITELLM_MASTER_KEY.
    model = LiteLLMModel(
        client_args={
            "api_key": LITELLM_MASTER_KEY,
            "api_base": LITELLM_BASE_URL,
            "use_litellm_proxy": True,
        },
        model_id=model_alias,
    )
    return Agent(
        model=model,
        tools=[get_fundamentals, get_price_history, search_news_semantic, search_news_fulltext],
        system_prompt=SYSTEM_PROMPT,
    )


def run_task(model: str, task: str, date_from: str | None, date_to: str | None) -> dict:
    user_content = task
    if date_from or date_to:
        user_content += f"\n\n(Scope date range: {date_from or 'earliest'} to {date_to or 'latest'})"

    agent = _build_agent(model)

    with tracer.start_as_current_span("agent.run_task") as root_span:
        root_span.set_attribute("agent.task", task[:200])
        root_span.set_attribute("agent.model", model)
        otel_trace_id = format(root_span.get_span_context().trace_id, "032x")

        try:
            result = agent(user_content, limits=Limits(turns=MAX_ITERATIONS))
        except Exception as e:
            # Covers e.g. MaxTokensReachedException and any transport-level
            # failure talking to the litellm proxy — the agent() call never
            # returns a result at all in this case, so no per-task metrics
            # can be recorded for it.
            LLM_CALL_COUNTER.labels(model=model, status="error").inc()
            log.error("llm call failed: %s", e, extra={"trace_id": otel_trace_id})
            raise TaskFailed(f"llm call failed: {e}", trace_id=otel_trace_id) from e

        metrics = result.metrics
        usage = metrics.accumulated_usage or {}
        prompt_tokens = usage.get("inputTokens", 0)
        completion_tokens = usage.get("outputTokens", 0)
        total_tokens = usage.get("totalTokens", prompt_tokens + completion_tokens)

        actual_model = ALIAS_TO_MODEL.get(model, model)
        root_span.set_attribute("agent.llm.actual_model", actual_model)

        try:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=actual_model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            )
            total_cost_usd = prompt_cost + completion_cost
        except Exception as e:
            log.warning(
                "cost calculation failed for model=%s: %s", actual_model, e, extra={"trace_id": otel_trace_id}
            )
            total_cost_usd = 0.0

        # Per-cycle LLM call metrics. One cycle == one LLM round trip (a
        # single response, however many tool calls it requested) — the same
        # granularity MAX_ITERATIONS bounds.
        # `metrics.latest_agent_invocation.cycles` gives per-cycle token
        # usage, aligned index-for-index with `metrics.cycle_durations` —
        # zipped here to add a span event per LLM call with its own
        # prompt/completion tokens and cost. Strands' own automatic `chat`
        # spans already carry per-call token usage (gen_ai.usage.* semconv
        # attributes) but never cost (Strands has no pricing knowledge) — a
        # span event on the root span fills that gap without duplicating
        # Strands' own spans.
        cycles = metrics.latest_agent_invocation.cycles
        for cycle_duration, cycle in zip(metrics.cycle_durations, cycles):
            cycle_prompt = cycle.usage.get("inputTokens", 0)
            cycle_completion = cycle.usage.get("outputTokens", 0)
            try:
                cp, cc = litellm.cost_per_token(
                    model=actual_model, prompt_tokens=cycle_prompt, completion_tokens=cycle_completion
                )
                cycle_cost = cp + cc
            except Exception:
                cycle_cost = 0.0
            root_span.add_event(
                "agent.llm_call",
                {
                    "agent.llm.prompt_tokens": cycle_prompt,
                    "agent.llm.completion_tokens": cycle_completion,
                    "agent.llm.cost_usd": cycle_cost,
                    "agent.llm.duration_s": cycle_duration,
                },
            )

            LLM_CALL_LATENCY.labels(model=actual_model).observe(cycle_duration)
            LLM_CALL_COUNTER.labels(model=actual_model, status="ok").inc()
            if cycle_duration > LLM_CALL_LATENCY_WARN_SECONDS:
                log.warning(
                    "slow llm call: model=%s duration=%.1fs task=%r",
                    actual_model, cycle_duration, task[:200],
                    extra={"trace_id": otel_trace_id},
                )

        LLM_TOKENS_COUNTER.labels(model=actual_model, type="prompt").inc(prompt_tokens)
        LLM_TOKENS_COUNTER.labels(model=actual_model, type="completion").inc(completion_tokens)
        LLM_COST_COUNTER.labels(model=actual_model).inc(total_cost_usd)

        # Per-tool metrics, from Strands' own tool_metrics (call_count/
        # success_count/error_count/total_time per tool name, aggregated
        # across the whole task) — per-call latency approximated as the
        # average, since Strands only exposes an aggregate total_time per
        # tool per task, not individual call timings. Status is "ok" or
        # "tool_error" — the exact error message/args per call aren't
        # available from Strands' aggregate counts, but the WARNING log
        # below still fires per error (task-level detail, not per-call) so
        # SemanticSearchFailing's `logs_link`
        # (observability/grafana/alerting/rules.yaml) still resolves to
        # something — that alert's Loki query matches on the
        # `tool_error tool=<name>` prefix, not exact message content.
        for tool_name, tm in metrics.tool_metrics.items():
            avg_time = tm.total_time / tm.call_count if tm.call_count else 0.0
            for _ in range(tm.success_count):
                TOOL_CALL_COUNTER.labels(tool=tool_name, status="ok").inc()
                TOOL_CALL_LATENCY.labels(tool=tool_name).observe(avg_time)
            for _ in range(tm.error_count):
                TOOL_CALL_COUNTER.labels(tool=tool_name, status="tool_error").inc()
                TOOL_CALL_LATENCY.labels(tool=tool_name).observe(avg_time)
            if tm.error_count:
                log.warning(
                    "tool_error tool=%s error_count=%d call_count=%d task=%r",
                    tool_name, tm.error_count, tm.call_count, task[:200],
                    extra={"trace_id": otel_trace_id},
                )

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

        root_span.set_attribute("agent.total_prompt_tokens", prompt_tokens)
        root_span.set_attribute("agent.total_completion_tokens", completion_tokens)
        root_span.set_attribute("agent.total_cost_usd", total_cost_usd)

        if result.stop_reason != "end_turn":
            # Covers limit_turns / limit_total_tokens / limit_output_tokens
            # (Strands' Limits) — a graceful stop without a final answer.
            # Metrics above are still recorded for this path.
            raise TaskFailed(
                f"agent stopped without a final answer (stop_reason={result.stop_reason}, "
                f"trace_id={otel_trace_id}, tokens used: {prompt_tokens} prompt + "
                f"{completion_tokens} completion, cost: ${total_cost_usd:.6f})",
                trace_id=otel_trace_id,
            )

        answer_text = "".join(
            block["text"] for block in result.message.get("content", []) if "text" in block
        )

        return {
            "answer": answer_text,
            "iterations": metrics.cycle_count,
            "otel_trace_id": otel_trace_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_usd": round(total_cost_usd, 6),
        }
