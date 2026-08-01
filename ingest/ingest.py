"""
One-shot ingestion job for the agent platform.

Runs to completion and exits. Idempotent: safe to re-run (e.g. because
`docker compose up` is run again) — it checks row counts / uses
ON CONFLICT DO NOTHING instead of blindly reloading.

Steps:
  1. Apply sql/schema.sql (tables, indexes, read-only role).
  2. Load each table that's still empty, from wherever DATA_SOURCE says:
       DATA_SOURCE=auto   (default) — use data/export/<table>.copy.gz if
                           present (fast path), else fall back to raw CSV.
                           This is why "raw" ingestion — the ~90s CSV parse
                           and the OpenAI embedding calls — only ever
                           happens once per table: the first successful run
                           writes the export, every run after finds it and
                           takes the fast path automatically.
       DATA_SOURCE=raw    — always (re-)parse data/raw/*.csv[.gz], ignoring
                             any existing export. Use this to force a fresh
                             raw ingestion, e.g. to embed a bigger subset
                             (see NEWS_EMBED_LIMIT below). The repo ships the
                             raw files gzipped (data/raw/*.csv.gz) — that's
                             transparently decompressed on read, see open_raw().
       DATA_SOURCE=export — always restore from data/export/<table>.copy.gz,
                             erroring out if it's missing rather than
                             silently falling back to raw.
  3. When loading news_headlines from raw:
       - Only the first NEWS_ROWS_LIMIT rows of the CSV are loaded into
         Postgres at all (default: all ~1.24M — the full-text search tool
         covers the whole corpus regardless of embedding limit below).
       - Of those, only NEWS_EMBED_LIMIT (default 20,000) get embedded via
         the LiteLLM embeddings endpoint (OpenAI text-embedding-3-small) —
         embedding the full 1.24M headlines is a real, costly, rate-limited
         batch job that does not belong in the default `docker compose up`
         path. If you want the full corpus embedded, run ingestion once,
         standalone, with a raised limit *before* `docker compose up`:
             NEWS_EMBED_LIMIT=1244184 DATA_SOURCE=raw \
                 docker compose run --rm ingest
         That writes data/export/news_headlines.copy.gz with the full
         embedding set; every subsequent `docker compose up` (DATA_SOURCE
         defaults to auto) picks it up automatically, no re-embedding.
       - Embedding is skipped gracefully (with a clear log message) if no
         OPENAI key/quota is available on the gateway — the agent still
         works via full-text search either way.
  4. If anything was freshly loaded from raw CSV/embedded this run, write
     fresh export files to data/export/ — nothing is written when tables
     were already populated or restored from an existing export, and an
     existing export file is never silently overwritten by a smaller/
     different raw run (e.g. a quick NEWS_ROWS_LIMIT=1000 dev iteration)
     unless EXPORT_OVERWRITE=true is set explicitly.
"""
import csv
import gzip
import logging
import os
import sys
import time
from pathlib import Path

import httpx
import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql as psql
from tenacity import retry, stop_after_attempt, wait_exponential

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/raw"))
EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", "/data/export"))
SCHEMA_FILE = Path(os.environ.get("SCHEMA_FILE", "/sql/schema.sql"))
DSN = os.environ["POSTGRES_DSN"]
# Required, no default — this is a real credential (the agent's read-only DB
# role's password) and should never silently fall back to a guessable value.
POSTGRES_AGENT_RO_PASSWORD = os.environ["POSTGRES_AGENT_RO_PASSWORD"]
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-local-master")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
NEWS_EMBED_LIMIT = int(os.environ.get("NEWS_EMBED_LIMIT", "20000"))
EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "100"))
# 0 (default) means "no cap, load all ~1.24M rows". Only trims how many rows
# land in Postgres from raw CSV; unrelated to NEWS_EMBED_LIMIT above.
NEWS_ROWS_LIMIT = int(os.environ.get("NEWS_ROWS_LIMIT", "0")) or None
DATA_SOURCE = os.environ.get("DATA_SOURCE", "auto")
if DATA_SOURCE not in ("raw", "export", "auto"):
    raise ValueError(f"DATA_SOURCE must be 'raw', 'export', or 'auto', got {DATA_SOURCE!r}")
# A raw run only overwrites an existing export/<table>.copy.gz if this is set —
# otherwise a smaller/different raw run (e.g. a quick NEWS_ROWS_LIMIT=1000 dev
# iteration) can never silently clobber a bigger export someone already produced
# (e.g. a full NEWS_EMBED_LIMIT=1244184 run).
EXPORT_OVERWRITE = os.environ.get("EXPORT_OVERWRITE", "false").lower() == "true"

FUNDAMENTALS_COLMAP = {
    "Ticker Symbol": "ticker",
    "Period Ending": "period_ending",
    "Accounts Payable": "accounts_payable",
    "Accounts Receivable": "accounts_receivable",
    "After Tax ROE": "after_tax_roe",
    "Capital Expenditures": "capital_expenditures",
    "Cash and Cash Equivalents": "cash_and_cash_equivalents",
    "Cost of Revenue": "cost_of_revenue",
    "Current Ratio": "current_ratio",
    "Earnings Before Interest and Tax": "earnings_before_interest_and_tax",
    "Earnings Before Tax": "earnings_before_tax",
    "Gross Margin": "gross_margin",
    "Gross Profit": "gross_profit",
    "Income Tax": "income_tax",
    "Net Income": "net_income",
    "Net Income Applicable to Common Shareholders": "net_income_applicable_to_common_shareholders",
    "Operating Income": "operating_income",
    "Operating Margin": "operating_margin",
    "Pre-Tax Margin": "pre_tax_margin",
    "Profit Margin": "profit_margin",
    "Research and Development": "research_and_development",
    "Total Assets": "total_assets",
    "Total Current Assets": "total_current_assets",
    "Total Current Liabilities": "total_current_liabilities",
    "Total Equity": "total_equity",
    "Total Liabilities": "total_liabilities",
    "Total Revenue": "total_revenue",
    "For Year": "for_year",
    "Earnings Per Share": "earnings_per_share",
    "Estimated Shares Outstanding": "estimated_shares_outstanding",
}


TABLE_COLUMNS = {
    "fundamentals": list(FUNDAMENTALS_COLMAP.values()),
    "prices": ["date", "symbol", "open", "close", "low", "high", "volume"],
    # search_vector is a GENERATED column and can't be COPYed into.
    "news_headlines": ["publish_date", "headline_text", "embedding"],
}


def export_path(table: str) -> Path:
    return EXPORT_DIR / f"{table}.copy.gz"


def has_export(table: str) -> bool:
    p = export_path(table)
    return p.exists() and p.stat().st_size > 0


def export_table(conn, table: str):
    if has_export(table) and not EXPORT_OVERWRITE:
        log.warning(
            "export: %s already exists and EXPORT_OVERWRITE is not set — leaving it as-is "
            "(this run's %s data was loaded into Postgres but NOT exported; set "
            "EXPORT_OVERWRITE=true to replace the existing export)",
            export_path(table), table,
        )
        return
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    cols = ", ".join(TABLE_COLUMNS[table])
    tmp_path = export_path(table).with_suffix(".gz.tmp")
    with conn.cursor() as cur, gzip.open(tmp_path, "wb") as gz:
        with cur.copy(f"COPY {table} ({cols}) TO STDOUT") as copy:
            for chunk in copy:
                gz.write(bytes(chunk))
    tmp_path.rename(export_path(table))
    log.info("export: wrote %s", export_path(table))


def import_table(conn, table: str):
    cols = ", ".join(TABLE_COLUMNS[table])
    with conn.cursor() as cur, gzip.open(export_path(table), "rb") as gz:
        with cur.copy(f"COPY {table} ({cols}) FROM STDIN") as copy:
            while chunk := gz.read(1024 * 1024):
                copy.write(chunk)
    conn.commit()
    log.info("import: restored %s from %s (table now has %d rows)", table, export_path(table), table_count(conn, table))


def export_all(conn, tables: list[str]):
    for table in tables:
        export_table(conn, table)


def num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def apply_schema(conn: psycopg.Connection):
    log.info("applying schema from %s", SCHEMA_FILE)
    sql_text = SCHEMA_FILE.read_text().replace("__POSTGRES_AGENT_RO_PASSWORD__", POSTGRES_AGENT_RO_PASSWORD)
    with conn.cursor() as cur:
        cur.execute(sql_text)
    conn.commit()


def table_count(conn, table):
    with conn.cursor() as cur:
        cur.execute(psql.SQL("SELECT count(*) FROM {}").format(psql.Identifier(table)))
        return cur.fetchone()[0]


def open_raw(filename: str):
    """
    Opens a raw data file for text reading, transparently gzip-decompressing
    if `<filename>.gz` exists — that's what actually ships in the repo (keeps
    git small while still tracking the real raw Kaggle source, not just our
    derived Postgres export). Falls back to the plain file if you've dropped
    a fresh, ungzipped CSV from Kaggle directly into data/raw/ yourself.
    """
    gz_path = DATA_DIR / f"{filename}.gz"
    if gz_path.exists():
        return gzip.open(gz_path, "rt", newline="")
    return open(DATA_DIR / filename, newline="")


def load_fundamentals(conn):
    expected_cols = list(FUNDAMENTALS_COLMAP.values())
    with open_raw("fundamentals.csv") as fh:
        reader = csv.DictReader(fh)
        rows = []
        for row in reader:
            record = {}
            for src, dst in FUNDAMENTALS_COLMAP.items():
                val = row.get(src)
                if dst in ("ticker", "period_ending"):
                    record[dst] = val
                elif dst == "for_year":
                    record[dst] = int(float(val)) if val else None
                else:
                    record[dst] = num(val)
            rows.append(record)

    insert_cols = expected_cols
    placeholders = ", ".join(f"%({c})s" for c in insert_cols)
    stmt = psql.SQL(
        "INSERT INTO fundamentals ({}) VALUES ({}) ON CONFLICT (ticker, period_ending) DO NOTHING"
    ).format(
        psql.SQL(", ").join(psql.Identifier(c) for c in insert_cols),
        psql.SQL(placeholders),
    )
    with conn.cursor() as cur:
        cur.executemany(stmt, rows)
    conn.commit()
    log.info("fundamentals: loaded %d rows (table now has %d)", len(rows), table_count(conn, "fundamentals"))


def load_prices_from_csv(conn):
    # COPY into a staging table then dedupe-insert, since plain COPY has no ON CONFLICT.
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE prices_staging (LIKE prices INCLUDING DEFAULTS)")
        cur.execute("ALTER TABLE prices_staging DROP COLUMN id")
    with open_raw("prices.csv") as fh, conn.cursor() as cur:
        with cur.copy(
            "COPY prices_staging (date, symbol, open, close, low, high, volume) FROM STDIN"
        ) as copy:
            reader = csv.reader(fh)
            next(reader)
            for row in reader:
                date, symbol, open_, close, low, high, volume = row
                copy.write_row([date.split(" ")[0], symbol, open_ or None, close or None,
                                 low or None, high or None, volume or None])
        cur.execute(
            "INSERT INTO prices (date, symbol, open, close, low, high, volume) "
            "SELECT date, symbol, open, close, low, high, volume FROM prices_staging "
            "ON CONFLICT (symbol, date) DO NOTHING"
        )
    conn.commit()
    log.info("prices: loaded (table now has %d rows)", table_count(conn, "prices"))


def load_news_from_csv(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE news_staging (publish_date TEXT, headline_text TEXT)")
    with open_raw("news_headlines.csv") as fh, conn.cursor() as cur:
        with cur.copy("COPY news_staging (publish_date, headline_text) FROM STDIN") as copy:
            reader = csv.reader(fh)
            next(reader)
            for i, row in enumerate(reader):
                if NEWS_ROWS_LIMIT is not None and i >= NEWS_ROWS_LIMIT:
                    break
                copy.write_row(row)
        cur.execute(
            "INSERT INTO news_headlines (publish_date, headline_text) "
            "SELECT to_date(publish_date, 'YYYYMMDD'), headline_text FROM news_staging"
        )
    conn.commit()
    log.info(
        "news_headlines: loaded (table now has %d rows, NEWS_ROWS_LIMIT=%s)",
        table_count(conn, "news_headlines"), NEWS_ROWS_LIMIT or "unlimited",
    )


@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=2, max=30))
def embed_batch(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    resp = client.post(
        "/embeddings",
        json={"model": EMBEDDING_MODEL, "input": texts},
        headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return [d["embedding"] for d in data]


def embed_news_subset(conn) -> bool:
    """Returns True if any rows were freshly embedded this run (i.e. should be (re-)exported)."""
    already = 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM news_headlines WHERE embedding IS NOT NULL")
        already = cur.fetchone()[0]
    if already >= NEWS_EMBED_LIMIT:
        log.info("news embeddings: already have %d embedded rows, skipping", already)
        return False

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, headline_text FROM news_headlines WHERE embedding IS NULL "
            "ORDER BY publish_date DESC LIMIT %s",
            (NEWS_EMBED_LIMIT - already,),
        )
        rows = cur.fetchall()

    if not rows:
        log.info("news embeddings: nothing left to embed")
        return False

    log.info("news embeddings: embedding %d headlines via %s", len(rows), EMBEDDING_MODEL)
    embedded_any = False
    with httpx.Client(base_url=LITELLM_BASE_URL) as client:
        for i in range(0, len(rows), EMBED_BATCH_SIZE):
            batch = rows[i : i + EMBED_BATCH_SIZE]
            ids = [r[0] for r in batch]
            texts = [r[1] for r in batch]
            try:
                vectors = embed_batch(client, texts)
            except Exception as e:
                log.error("embedding batch failed after retries: %s", e)
                if not embedded_any and i == 0:
                    log.warning(
                        "first embedding batch failed — likely no OPENAI_API_KEY/quota configured on "
                        "the litellm gateway. Skipping semantic embedding entirely; search_news_fulltext "
                        "still covers all headlines."
                    )
                    return False
                continue
            embedded_any = True
            with conn.cursor() as cur:
                for row_id, vec in zip(ids, vectors):
                    cur.execute(
                        "UPDATE news_headlines SET embedding = %s WHERE id = %s",
                        (vec, row_id),
                    )
            conn.commit()
            log.info("news embeddings: %d/%d done", min(i + EMBED_BATCH_SIZE, len(rows)), len(rows))

    if not embedded_any:
        log.warning("news embeddings: no rows were successfully embedded, skipping ANN index build")
        return False

    with conn.cursor() as cur:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_news_embedding ON news_headlines "
            "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100) "
            "WHERE embedding IS NOT NULL"
        )
    conn.commit()
    log.info("news embeddings: ANN index built")
    return True


def wait_for_postgres():
    for attempt in range(30):
        try:
            with psycopg.connect(DSN, connect_timeout=3) as conn:
                return
        except Exception:
            log.info("waiting for postgres... (%d)", attempt)
            time.sleep(2)
    log.error("postgres never became available")
    sys.exit(1)


def load_table_if_empty(conn, table: str, load_from_csv) -> bool:
    """
    Loads `table` if it's currently empty, from wherever DATA_SOURCE says.
    Returns True iff it was freshly loaded from raw CSV (i.e. should be exported).
    """
    if table_count(conn, table) > 0:
        log.info("%s: already loaded, skipping", table)
        return False

    if DATA_SOURCE == "export":
        if not has_export(table):
            raise RuntimeError(
                f"DATA_SOURCE=export but no export file found at {export_path(table)} — "
                f"run once with DATA_SOURCE=raw (or auto) first to produce it"
            )
        import_table(conn, table)
        return False

    if DATA_SOURCE == "auto" and has_export(table):
        import_table(conn, table)
        return False

    load_from_csv(conn)
    return True


def main():
    log.info("DATA_SOURCE=%s NEWS_EMBED_LIMIT=%d NEWS_ROWS_LIMIT=%s", DATA_SOURCE, NEWS_EMBED_LIMIT, NEWS_ROWS_LIMIT or "unlimited")
    wait_for_postgres()
    with psycopg.connect(DSN, autocommit=False) as conn:
        apply_schema(conn)
        register_vector(conn)

        dirty = set()
        if load_table_if_empty(conn, "fundamentals", load_fundamentals):
            dirty.add("fundamentals")
        if load_table_if_empty(conn, "prices", load_prices_from_csv):
            dirty.add("prices")
        if load_table_if_empty(conn, "news_headlines", load_news_from_csv):
            dirty.add("news_headlines")

        # Only attempt (rate-limited, costly) embedding when this run actually
        # loaded fresh headlines from CSV — an export-sourced news table
        # already carries whatever embeddings its source run produced.
        if "news_headlines" in dirty:
            embed_news_subset(conn)

        if dirty:
            log.info("exporting freshly loaded tables for fast restore next run: %s", sorted(dirty))
            export_all(conn, sorted(dirty))
        else:
            log.info("nothing freshly loaded this run, skipping export")

    log.info("ingest complete")


if __name__ == "__main__":
    main()
