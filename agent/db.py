import os

from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool


def _configure(conn):
    register_vector(conn)


def make_pool() -> ConnectionPool:
    dsn = os.environ["POSTGRES_DSN"]
    pool = ConnectionPool(dsn, min_size=1, max_size=10, configure=_configure, open=True)
    pool.wait(timeout=30)
    return pool
