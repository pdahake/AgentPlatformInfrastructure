import logging
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from openai import OpenAI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from agent_loop import TaskFailed, run_task
from db import make_pool
from observability import TASK_COUNTER, TASK_LATENCY, setup_logging, setup_tracing
from tools import Tools

setup_logging()
log = logging.getLogger("app")
tracer = setup_tracing()

AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-agent")
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]

app = FastAPI(title="agent-platform")
FastAPIInstrumentor.instrument_app(app)

pool = make_pool()
llm_client = OpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_MASTER_KEY, max_retries=1)
tools = Tools(pool=pool, litellm_client=llm_client)


class RunRequest(BaseModel):
    task: str
    date_from: str | None = None
    date_to: str | None = None
    model: str | None = None


class RunResponse(BaseModel):
    answer: str
    iterations: int
    trace_id: str
    duration_seconds: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float


@app.get("/health")
def health():
    try:
        with pool.connection(timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db unavailable: {e}")
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/run", response_model=RunResponse)
def run(req: RunRequest):
    if not req.task or not req.task.strip():
        raise HTTPException(status_code=400, detail="task must not be empty")

    model = req.model or AGENT_MODEL
    start = time.monotonic()
    log.info("task started model=%s task=%r", model, req.task[:200])

    try:
        result = run_task(llm_client, model, tools, req.task, req.date_from, req.date_to)
    except TaskFailed as e:
        TASK_COUNTER.labels(status="failed").inc()
        log.error("task failed error=%s", e, extra={"trace_id": e.trace_id})
        raise HTTPException(status_code=502, detail=str(e))

    duration = time.monotonic() - start
    TASK_LATENCY.observe(duration)
    TASK_COUNTER.labels(status="ok").inc()
    trace_id = result["otel_trace_id"]
    log.info(
        "task completed trace_id=%s duration=%.2fs iterations=%d total_tokens=%d cost_usd=%.6f",
        trace_id, duration, result["iterations"], result["total_tokens"], result["cost_usd"],
        extra={"trace_id": trace_id},
    )

    return RunResponse(
        answer=result["answer"] or "",
        iterations=result["iterations"],
        trace_id=trace_id,
        duration_seconds=duration,
        prompt_tokens=result["prompt_tokens"],
        completion_tokens=result["completion_tokens"],
        total_tokens=result["total_tokens"],
        cost_usd=result["cost_usd"],
    )
