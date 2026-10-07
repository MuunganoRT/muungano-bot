"""Where Duma keeps its own state: sessions and pending confirmations.

PostgreSQL when `BOT_DATABASE_URL` is set, or else the database Cloudron gives
the app (`CLOUDRON_POSTGRESQL_URL`); inside a schema of its own, so its tables
never sit among the API's. With neither, a SQLite file in `state/`, which is
what the tests and a first local run use.

Statements are written once, with `$1` placeholders and types both engines
take. Every statement in this package is fixed text: nothing the model or an
admin writes is ever part of the SQL.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Protocol

_PLACEHOLDER = re.compile(r"\$(\d+)")
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class DatabaseError(Exception):
    """The database cannot be used, with a message that names no credential."""


class Database(Protocol):
    name: str

    async def fetch(self, sql: str, *params: Any) -> list[tuple]: ...

    async def execute(self, sql: str, *params: Any) -> int:
        """Run a statement and return how many rows it changed."""
        ...

    async def close(self) -> None: ...


class SqliteDatabase:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, timeout=10)
        path.chmod(0o600)
        self.name = f"SQLite file {path}"

    async def fetch(self, sql: str, *params: Any) -> list[tuple]:
        return self._db.execute(_PLACEHOLDER.sub(r"?\1", sql), params).fetchall()

    async def execute(self, sql: str, *params: Any) -> int:
        return self._db.execute(_PLACEHOLDER.sub(r"?\1", sql), params).rowcount

    async def close(self) -> None:
        self._db.close()


class PostgresDatabase:
    def __init__(self, pool: Any, schema: str):
        self._pool = pool
        self.schema = schema
        self.name = f"PostgreSQL, schema {schema}"

    @classmethod
    async def connect(cls, url: str, schema: str) -> "PostgresDatabase":
        import asyncpg  # imported here so the bot runs on SQLite without the driver

        if not _SCHEMA_NAME.match(schema):
            raise DatabaseError("BOT_DATABASE_SCHEMA must be a plain lowercase name")
        try:
            # Unqualified table names resolve inside the bot's schema and nowhere else.
            pool = await asyncpg.create_pool(
                url, min_size=1, max_size=2, command_timeout=10, server_settings={"search_path": schema}
            )
            exists = await pool.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", schema)
            if not exists:
                await pool.execute(f'CREATE SCHEMA "{schema}"')
        except (asyncpg.PostgresError, OSError) as exc:
            # The exception text can carry the connection string: keep only its class.
            raise DatabaseError(f"cannot use the PostgreSQL database it was given ({type(exc).__name__})") from None
        return cls(pool, schema)

    async def fetch(self, sql: str, *params: Any) -> list[tuple]:
        return [tuple(row) for row in await self._pool.fetch(sql, *params)]

    async def execute(self, sql: str, *params: Any) -> int:
        status = await self._pool.execute(sql, *params)
        last = status.rsplit(" ", 1)[-1]
        return int(last) if last.isdigit() else 0

    async def close(self) -> None:
        await self._pool.close()

    async def foreign_tables(self) -> int:
        """How many tables outside its own schema this user can read. 0 when Duma has a role of its own."""
        rows = await self.fetch(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema', $1) "
            "AND has_table_privilege(quote_ident(table_schema) || '.' || quote_ident(table_name), 'SELECT')",
            self.schema,
        )
        return rows[0][0]
