import pytest

from duma.config import ConfigError, load


def test_loads_and_parses(settings):
    assert settings.admin_chat_id == -1001234567890
    assert settings.allowed_user_ids == frozenset({10, 20})
    assert settings.api_url == "http://api.test"  # trailing slash removed
    assert settings.model == "claude-sonnet-5-5"
    assert settings.session_max_tokens == 100_000


def test_secrets_stay_out_of_repr(settings):
    shown = repr(settings)
    for secret in ("FAKE-TOKEN-FOR-TESTS", "sk-fake-for-tests", "t" * 40):
        assert secret not in shown


@pytest.mark.parametrize(
    "name",
    ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_CHAT_ID", "TELEGRAM_ALLOWED_USER_IDS", "BOT_API_URL", "BOT_API_TOKEN", "ANTHROPIC_API_KEY"],
)
def test_a_missing_variable_is_named(env, name):
    env.pop(name)
    with pytest.raises(ConfigError, match=name):
        load(env=env, env_file=None)


def test_errors_never_carry_values(env):
    env["BOT_API_TOKEN"] = "short-secret"
    with pytest.raises(ConfigError) as excinfo:
        load(env=env, env_file=None)
    assert "short-secret" not in str(excinfo.value)
    assert "BOT_API_TOKEN" in str(excinfo.value)


def test_bad_ids(env):
    env["TELEGRAM_ALLOWED_USER_IDS"] = "10,abc"
    with pytest.raises(ConfigError, match="TELEGRAM_ALLOWED_USER_IDS"):
        load(env=env, env_file=None)
    env["TELEGRAM_ALLOWED_USER_IDS"] = " , "
    with pytest.raises(ConfigError, match="empty"):
        load(env=env, env_file=None)
    env["TELEGRAM_ALLOWED_USER_IDS"] = "10"
    env["TELEGRAM_ADMIN_CHAT_ID"] = "not-a-number"
    with pytest.raises(ConfigError, match="TELEGRAM_ADMIN_CHAT_ID"):
        load(env=env, env_file=None)


def test_the_environment_wins_over_the_file(env, tmp_path):
    file = tmp_path / "test.env"
    file.write_text("BOT_MODEL=from-file\nBOT_MAX_TURNS=3\n", encoding="utf-8")
    s = load(env={**env, "BOT_MODEL": "from-env"}, env_file=file)
    assert s.model == "from-env" and s.max_turns == 3


def test_the_database_is_the_bots_own_url_else_cloudrons_else_none(env):
    assert load(env=env, env_file=None).database_url == ""
    cloudron = {**env, "CLOUDRON_POSTGRESQL_URL": "postgres://app:x@postgresql/db"}
    assert load(env=cloudron, env_file=None).database_url == "postgres://app:x@postgresql/db"
    both = {**cloudron, "BOT_DATABASE_URL": "postgresql://duma:y@localhost/muungano"}
    settings = load(env=both, env_file=None)
    assert settings.database_url == "postgresql://duma:y@localhost/muungano" and settings.database_schema == "duma"
    assert "duma:y" not in repr(settings)  # the URL carries a password
