import os
import secrets

import pytest

from duma.config import load

# Fake values only: no test ever touches a real credential.
FAKE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:FAKE-TOKEN-FOR-TESTS",
    "TELEGRAM_ADMIN_CHAT_ID": "-1001234567890",
    "TELEGRAM_ALLOWED_USER_IDS": "10, 20",
    "BOT_API_URL": "http://api.test/",
    "BOT_API_TOKEN": "t" * 40,
    "ANTHROPIC_API_KEY": "sk-fake-for-tests",
    "BOT_ACTION_DELAY_S": "0",
    "BOT_HOOK_PORT": "0",
}


@pytest.fixture
def env():
    return dict(FAKE_ENV)


@pytest.fixture
def settings(env, tmp_path):
    return load(env={**env, "BOT_STATE_DIR": str(tmp_path / "state")}, env_file=None)


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    """Duma's state database, on each engine. PostgreSQL runs only when DUMA_TEST_DATABASE_URL points at one."""
    from duma.database import PostgresDatabase, SqliteDatabase

    if request.param == "sqlite":
        database = SqliteDatabase(tmp_path / "state" / "bot.sqlite")
        yield database
        await database.close()
        return
    url = os.environ.get("DUMA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("DUMA_TEST_DATABASE_URL is not set")
    schema = "t_" + secrets.token_hex(6)  # a schema of its own per test, dropped afterwards
    database = await PostgresDatabase.connect(url, schema)
    yield database
    await database.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await database.close()


@pytest.fixture
def sqlite_db(tmp_path):
    from duma.database import SqliteDatabase

    return SqliteDatabase(tmp_path / "bot.sqlite")
