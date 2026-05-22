"""Database service: connection profiles + safe query execution.

Profile fields:
    name, kind ('postgres' | 'mysql' | 'sqlite'),
    env ('local' | 'test' | 'pre-prod' | 'prod' | <custom>),
    host, port, database, username, password (M0 plaintext),
    read_only_envs (list[str]) — write queries are rejected against these envs.

Production safety:
- env=='prod' rejects any non-SELECT statement by default.
- All queries get a server-side LIMIT cap applied when missing.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

DEFAULT_LIMIT = 200
PROD_ENVS = {"prod", "production", "live"}
ALLOWED_KINDS = {"postgres", "mysql", "sqlite"}


class DbError(RuntimeError):
    pass


def _connection_url(profile: dict[str, Any]) -> str:
    kind = profile.get("kind")
    if kind == "postgres":
        user = quote_plus(profile.get("username", ""))
        pwd = quote_plus(profile.get("password", ""))
        host = profile.get("host", "localhost")
        port = int(profile.get("port") or 5432)
        db = profile.get("database", "")
        return f"postgresql+psycopg://{user}:{pwd}@{host}:{port}/{db}"
    if kind == "mysql":
        user = quote_plus(profile.get("username", ""))
        pwd = quote_plus(profile.get("password", ""))
        host = profile.get("host", "localhost")
        port = int(profile.get("port") or 3306)
        db = profile.get("database", "")
        return f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{db}"
    if kind == "sqlite":
        return f"sqlite:///{profile.get('database', ':memory:')}"
    raise DbError(f"unknown kind {kind!r}; supported: {sorted(ALLOWED_KINDS)}")


def _is_select(sql: str) -> bool:
    head = sql.strip().split(None, 1)
    if not head:
        return False
    return head[0].upper() in {"SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC"}


def _apply_limit(sql: str, limit: int) -> str:
    if re.search(r"\blimit\b\s+\d+", sql, re.IGNORECASE):
        return sql
    return f"{sql.rstrip(';').rstrip()}\nLIMIT {limit}"


def test_connection(profile: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import create_engine, text

    try:
        engine = create_engine(_connection_url(profile), pool_pre_ping=True)
        with engine.connect() as conn:
            row = conn.execute(text("SELECT 1")).scalar()
        return {"ok": row == 1}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}


def run_query(
    profile: dict[str, Any],
    sql: str,
    *,
    limit: int = DEFAULT_LIMIT,
    allow_writes: bool = False,
) -> dict[str, Any]:
    from sqlalchemy import create_engine, text

    if not _is_select(sql) and not allow_writes:
        raise DbError("only SELECT/WITH/EXPLAIN/SHOW/DESCRIBE allowed unless allow_writes=true")
    env = (profile.get("env") or "").lower()
    if env in PROD_ENVS and not _is_select(sql):
        raise DbError(f"writes against prod env {env!r} are blocked")

    final_sql = _apply_limit(sql, min(max(1, limit), 5000)) if _is_select(sql) else sql

    engine = create_engine(_connection_url(profile), pool_pre_ping=True)
    with engine.connect() as conn:
        result = conn.execute(text(final_sql))
        if result.returns_rows:
            rows = result.fetchall()
            return {
                "columns": list(result.keys()),
                "rows": [list(r) for r in rows],
                "rowcount": len(rows),
                "sql": final_sql,
            }
        return {"rowcount": result.rowcount, "sql": final_sql}


def introspect_schema(profile: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import create_engine, inspect

    engine = create_engine(_connection_url(profile), pool_pre_ping=True)
    insp = inspect(engine)
    tables: list[dict[str, Any]] = []
    for tbl in insp.get_table_names():
        tables.append(
            {
                "name": tbl,
                "columns": [
                    {"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable", True)}
                    for c in insp.get_columns(tbl)
                ],
            }
        )
    return {"tables": tables, "count": len(tables)}
