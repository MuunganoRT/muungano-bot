import pytest

from duma.database import DatabaseError, PostgresDatabase, SqliteDatabase


async def test_statements_and_row_counts_behave_the_same_on_both_engines(db):
    await db.execute("CREATE TABLE things (id BIGINT PRIMARY KEY, label TEXT NOT NULL, weight DOUBLE PRECISION NOT NULL)")
    assert await db.execute("INSERT INTO things (id, label, weight) VALUES ($1, $2, $3)", 5_000_000_000, "ñu", 1.5) == 1
    assert await db.execute("UPDATE things SET label = $2 WHERE id = $1", 5_000_000_000, "gnu") == 1
    assert await db.execute("UPDATE things SET label = $2 WHERE id = $1", 7, "nadie") == 0
    assert await db.fetch("SELECT id, label, weight FROM things WHERE weight > $1", 1.0) == [(5_000_000_000, "gnu", 1.5)]
    assert await db.execute("DELETE FROM things WHERE id = $1", 5_000_000_000) == 1


async def test_the_sqlite_file_is_private(tmp_path):
    SqliteDatabase(tmp_path / "state" / "bot.sqlite")
    assert (tmp_path / "state" / "bot.sqlite").stat().st_mode & 0o777 == 0o600


async def test_a_database_that_cannot_be_reached_fails_without_showing_the_url():
    with pytest.raises(DatabaseError) as caught:
        await PostgresDatabase.connect("postgresql://duma:secreto@127.0.0.1:1/nada", "duma")
    assert "secreto" not in str(caught.value) and "127.0.0.1" not in str(caught.value)
    with pytest.raises(DatabaseError, match="BOT_DATABASE_SCHEMA"):
        await PostgresDatabase.connect("postgresql://x", 'duma"; drop schema public')
