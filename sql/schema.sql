-- Schema for the agent platform's local data store.
-- Applied once by the `ingest` service. Two Postgres roles are created here
-- so ingestion (DDL + writes) and the agent (read-only) have distinct
-- privileges — the agent can never mutate data even if a tool implementation
-- has a bug or the LLM tries to coerce it into doing so.

CREATE EXTENSION IF NOT EXISTS vector;

-- __POSTGRES_AGENT_RO_PASSWORD__ is substituted by ingest.py's apply_schema() from the
-- POSTGRES_AGENT_RO_PASSWORD env var before this file is executed — never a literal
-- credential in this tracked file. Role creation is idempotent (skips if it
-- already exists); the password is set unconditionally every run via ALTER
-- ROLE, so — unlike POSTGRES_PASSWORD, which Postgres only applies on a truly
-- fresh volume — changing POSTGRES_AGENT_RO_PASSWORD in .env and re-running ingest
-- actually takes effect, no volume wipe needed.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_ro') THEN
        CREATE ROLE agent_ro LOGIN;
    END IF;
END
$$;
ALTER ROLE agent_ro PASSWORD '__POSTGRES_AGENT_RO_PASSWORD__';

-- Same pattern as agent_ro above, for postgres_exporter (the Prometheus DB
-- metrics scraper). Granted the built-in `pg_monitor` role rather than table
-- SELECTs — pg_monitor is Postgres's own least-privilege bundle for exactly
-- this purpose (pg_stat_*/pg_settings visibility, no access to actual table
-- data), so the exporter can see connection counts/cache hit ratio/tuple
-- stats without ever being able to read fundamentals/prices/news_headlines.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'postgres_exporter') THEN
        CREATE ROLE postgres_exporter LOGIN;
    END IF;
END
$$;
ALTER ROLE postgres_exporter PASSWORD '__POSTGRES_EXPORTER_PASSWORD__';
GRANT pg_monitor TO postgres_exporter;

CREATE TABLE IF NOT EXISTS fundamentals (
    id                          SERIAL PRIMARY KEY,
    ticker                      TEXT NOT NULL,
    period_ending               DATE NOT NULL,
    accounts_payable            NUMERIC,
    accounts_receivable         NUMERIC,
    after_tax_roe               NUMERIC,
    capital_expenditures        NUMERIC,
    cash_and_cash_equivalents   NUMERIC,
    cost_of_revenue             NUMERIC,
    current_ratio               NUMERIC,
    earnings_before_interest_and_tax NUMERIC,
    earnings_before_tax         NUMERIC,
    gross_margin                NUMERIC,
    gross_profit                NUMERIC,
    income_tax                  NUMERIC,
    net_income                  NUMERIC,
    net_income_applicable_to_common_shareholders NUMERIC,
    operating_income            NUMERIC,
    operating_margin            NUMERIC,
    pre_tax_margin              NUMERIC,
    profit_margin               NUMERIC,
    research_and_development    NUMERIC,
    total_assets                NUMERIC,
    total_current_assets        NUMERIC,
    total_current_liabilities   NUMERIC,
    total_equity                NUMERIC,
    total_liabilities           NUMERIC,
    total_revenue                NUMERIC,
    for_year                    INTEGER,
    earnings_per_share          NUMERIC,
    estimated_shares_outstanding NUMERIC,
    UNIQUE (ticker, period_ending)
);
CREATE INDEX IF NOT EXISTS idx_fundamentals_ticker ON fundamentals (ticker);

CREATE TABLE IF NOT EXISTS prices (
    id      BIGSERIAL PRIMARY KEY,
    date    DATE NOT NULL,
    symbol  TEXT NOT NULL,
    open    NUMERIC,
    close   NUMERIC,
    low     NUMERIC,
    high    NUMERIC,
    volume  NUMERIC,
    UNIQUE (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_symbol_date ON prices (symbol, date);

CREATE TABLE IF NOT EXISTS news_headlines (
    id            BIGSERIAL PRIMARY KEY,
    publish_date  DATE NOT NULL,
    headline_text TEXT NOT NULL,
    embedding     vector(1536),
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', headline_text)) STORED
);
CREATE INDEX IF NOT EXISTS idx_news_publish_date ON news_headlines (publish_date);
CREATE INDEX IF NOT EXISTS idx_news_search_vector ON news_headlines USING GIN (search_vector);
-- The ivfflat ANN index on `embedding` is created by ingest.py AFTER the
-- embedding subset is populated (ivfflat clusters need real data to train
-- on; building it against an empty column produces a low-quality index).

GRANT CONNECT ON DATABASE agent_platform TO agent_ro;
GRANT USAGE ON SCHEMA public TO agent_ro;
GRANT SELECT ON fundamentals, prices, news_headlines TO agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agent_ro;
