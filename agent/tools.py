"""
The agent's fixed tool surface.

Deliberately NOT text-to-SQL: the LLM never writes or sees raw SQL. Each
tool is a typed Python function with validated parameters, executed against
a read-only Postgres role (agent_ro). This bounds what a prompt-injected or
misbehaving model can do to "query these four things," not "run anything."
"""
from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any

from openai import OpenAI
from psycopg_pool import ConnectionPool

TICKER_RE = re.compile(r"^[A-Z.\-]{1,10}$")

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")


class ToolError(Exception):
    pass


def _validate_ticker(ticker: str) -> str:
    ticker = ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        raise ToolError(f"invalid ticker format: {ticker!r}")
    return ticker


def _validate_date(value: str | None, field: str) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"invalid {field}, expected YYYY-MM-DD: {value!r}")


class Tools:
    def __init__(self, pool: ConnectionPool, litellm_client: OpenAI):
        self.pool = pool
        self.litellm = litellm_client

    # -- market data -----------------------------------------------------

    def get_fundamentals(self, ticker: str, limit: int = 8) -> list[dict[str, Any]]:
        ticker = _validate_ticker(ticker)
        limit = max(1, min(limit, 40))
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT period_ending, total_revenue, net_income, gross_margin,
                       operating_margin, profit_margin, earnings_per_share,
                       total_assets, total_liabilities, total_equity, current_ratio
                FROM fundamentals
                WHERE ticker = %s
                ORDER BY period_ending DESC
                LIMIT %s
                """,
                (ticker, limit),
            )
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        if not rows:
            raise ToolError(f"no fundamentals found for ticker {ticker!r}")
        for r in rows:
            r["period_ending"] = r["period_ending"].isoformat()
        return rows

    def get_price_history(
        self, ticker: str, date_from: str | None = None, date_to: str | None = None, limit: int = 60
    ) -> list[dict[str, Any]]:
        ticker = _validate_ticker(ticker)
        d_from = _validate_date(date_from, "date_from")
        d_to = _validate_date(date_to, "date_to")
        limit = max(1, min(limit, 500))

        clauses = ["symbol = %s"]
        params: list[Any] = [ticker]
        if d_from:
            clauses.append("date >= %s")
            params.append(d_from)
        if d_to:
            clauses.append("date <= %s")
            params.append(d_to)
        where = " AND ".join(clauses)
        params.append(limit)

        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT date, open, close, low, high, volume
                FROM prices
                WHERE {where}
                ORDER BY date DESC
                LIMIT %s
                """,
                params,
            )
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        if not rows:
            raise ToolError(f"no price history found for ticker {ticker!r} in that range")
        for r in rows:
            r["date"] = r["date"].isoformat()
        return rows

    # -- news --------------------------------------------------------------

    def search_news_semantic(
        self, query: str, k: int = 8, date_from: str | None = None, date_to: str | None = None
    ) -> list[dict[str, Any]]:
        if not query or not query.strip():
            raise ToolError("query must not be empty")
        k = max(1, min(k, 25))
        d_from = _validate_date(date_from, "date_from")
        d_to = _validate_date(date_to, "date_to")

        # Fail fast instead of paying for an embeddings call when the ingest step
        # never populated any embeddings (e.g. no OpenAI quota) — an empty-corpus
        # semantic search is never useful, and this keeps a misconfigured embedding
        # provider from burning agent loop iterations on retries/timeouts.
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM news_headlines WHERE embedding IS NOT NULL)")
            if not cur.fetchone()[0]:
                raise ToolError(
                    "no headlines have been embedded (semantic search index is empty) — "
                    "use search_news_fulltext instead"
                )

        embedding = (
            self.litellm.embeddings.create(model=EMBEDDING_MODEL, input=[query]).data[0].embedding
        )

        clauses = ["embedding IS NOT NULL"]
        date_params: list[Any] = []
        if d_from:
            clauses.append("publish_date >= %s")
            date_params.append(d_from)
        if d_to:
            clauses.append("publish_date <= %s")
            date_params.append(d_to)
        where = " AND ".join(clauses)

        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT publish_date, headline_text, embedding <=> %s AS distance
                FROM news_headlines
                WHERE {where}
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                (embedding, *date_params, embedding, k),
            )
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            r["publish_date"] = r["publish_date"].isoformat()
            r["distance"] = round(float(r["distance"]), 4)
        if not rows:
            raise ToolError(
                "no semantically-embedded headlines matched (embedding subset may not cover this "
                "date range) — try search_news_fulltext instead"
            )
        return rows

    def search_news_fulltext(
        self, query: str, k: int = 8, date_from: str | None = None, date_to: str | None = None
    ) -> list[dict[str, Any]]:
        if not query or not query.strip():
            raise ToolError("query must not be empty")
        k = max(1, min(k, 25))
        d_from = _validate_date(date_from, "date_from")
        d_to = _validate_date(date_to, "date_to")

        clauses = ["search_vector @@ websearch_to_tsquery('english', %s)"]
        params: list[Any] = [query]
        if d_from:
            clauses.append("publish_date >= %s")
            params.append(d_from)
        if d_to:
            clauses.append("publish_date <= %s")
            params.append(d_to)
        where = " AND ".join(clauses)
        params.append(k)

        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT publish_date, headline_text
                FROM news_headlines
                WHERE {where}
                ORDER BY publish_date DESC
                LIMIT %s
                """,
                params,
            )
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        if not rows:
            raise ToolError(f"no headlines matched {query!r} in that range")
        for r in rows:
            r["publish_date"] = r["publish_date"].isoformat()
        return rows


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals",
            "description": "Get recent financial-statement fundamentals for a stock ticker (revenue, margins, EPS, balance sheet), most recent period first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. AAPL"},
                    "limit": {"type": "integer", "description": "Max periods to return (default 8)"},
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_price_history",
            "description": "Get daily OHLCV price history for a stock ticker, optionally scoped to a date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. AAPL"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                    "limit": {"type": "integer", "description": "Max rows to return (default 60)"},
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_news_semantic",
            "description": "Semantic search over a recent-weighted subset of news headlines using embeddings. Best for conceptual/topical queries. Optionally scoped to a date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "description": "Max results (default 8)"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_news_fulltext",
            "description": "Keyword full-text search over ALL 1.24M news headlines (not just the embedded subset). Best for exact terms/names, or when semantic search returns nothing for the requested date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "description": "Max results (default 8)"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                },
                "required": ["query"],
            },
        },
    },
]
