import json

import pytest

from layercfg import ConfigError, MissingSetting, load_settings
from layercfg.defaults import DEFAULTS


def write(tmp_path, data):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_defaults_only():
    s = load_settings(env={})
    assert s.get("server.port") == 8080
    assert s.get("database.pool.max_size") == 10
    assert s.get("cache.redis.retry.attempts") == 3


def test_missing_file_is_ignored(tmp_path):
    s = load_settings(str(tmp_path / "nope.json"), env={})
    assert s.get("app.name") == "service"


def test_file_overrides_top_level_section_key(tmp_path):
    s = load_settings(write(tmp_path, {"server": {"port": 9000}}), env={})
    assert s.get("server.port") == 9000
    assert s.get("server.host") == "127.0.0.1"
    assert s.get("server.workers") == 2


def test_file_adds_new_section(tmp_path):
    s = load_settings(write(tmp_path, {"features": {"beta": True}}), env={})
    assert s.get("features.beta") is True


def test_env_overrides_file(tmp_path):
    path = write(tmp_path, {"app": {"log_level": "DEBUG"}})
    s = load_settings(path, env={"APP__APP__LOG_LEVEL": "WARNING", "APP__APP__DEBUG": "yes"})
    assert s.get("app.log_level") == "WARNING"
    assert s.get("app.debug") is True
    assert s.get("app.name") == "service"


def test_explicit_overrides_win(tmp_path):
    s = load_settings(env={"APP__SERVER__PORT": "7000"}, overrides={"server": {"port": 6000}})
    assert s.get("server.port") == 6000


def test_invalid_json(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        load_settings(str(path), env={})


def test_top_level_must_be_object(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, [1, 2]), env={})


def test_missing_setting():
    s = load_settings(env={})
    with pytest.raises(MissingSetting):
        s.get("server.nope")
    assert s.get("server.nope", default=5) == 5
    assert "server.port" in s
    assert "server.nope" not in s


def test_section_view():
    s = load_settings(env={})
    pool = s.section("database.pool")
    assert pool.get("timeout") == 30.0
    with pytest.raises(ConfigError):
        s.section("server.port")


def test_defaults_not_mutated(tmp_path):
    load_settings(write(tmp_path, {"server": {"port": 1}}), env={"APP__APP__NAME": "x"})
    assert DEFAULTS["server"]["port"] == 8080
    assert DEFAULTS["app"]["name"] == "service"
