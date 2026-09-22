"""Database connection helper.

One place that knows how to open a connection, reading DATABASE_URL from
the environment (Railway sets this automatically for a linked Postgres
service). No connection pooling yet — added when there's a real server
process to pool for; a one-off script or test just opens and closes.
"""
import os
import psycopg


def get_connection():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Locally, run via `railway run` "
            "(or export it from `railway variables`) so it's injected."
        )
    return psycopg.connect(url)
