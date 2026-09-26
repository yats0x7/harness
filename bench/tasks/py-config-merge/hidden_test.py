import json

from layercfg import load_settings
from layercfg.defaults import DEFAULTS
from layercfg.merge import merge


def write(tmp_path, data):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_file_override_of_nested_section_keeps_sibling_defaults(tmp_path):
    s = load_settings(write(tmp_path, {"database": {"pool": {"max_size": 50}}}), env={})
    assert s.get("database.pool.max_size") == 50
    assert s.get("database.pool.timeout") == 30.0
    assert s.get("database.pool.min_size") == 1
    assert s.get("database.pool.recycle") == 1800
    assert s.get("database.url") == "sqlite:///app.db"


def test_env_override_of_nested_section_keeps_sibling_defaults():
    s = load_settings(env={"APP__SERVER__TLS__ENABLED": "true"})
    assert s.get("server.tls.enabled") is True
    assert s.get("server.tls.min_version") == "TLSv1.2"
    assert s.get("server.port") == 8080


def test_three_layers_of_nesting(tmp_path):
    path = write(tmp_path, {"cache": {"redis": {"retry": {"attempts": 10}}}})
    s = load_settings(path, env={"APP__CACHE__REDIS__URL": "redis://cache:6379/1"})
    assert s.get("cache.redis.retry.attempts") == 10
    assert s.get("cache.redis.retry.backoff") == 0.5
    assert s.get("cache.redis.url") == "redis://cache:6379/1"
    assert s.get("cache.ttl") == 300


def test_file_and_env_touch_different_keys_of_same_nested_section(tmp_path):
    path = write(tmp_path, {"database": {"pool": {"max_size": 20}}})
    s = load_settings(path, env={"APP__DATABASE__POOL__TIMEOUT": "5"})
    assert s.get("database.pool.max_size") == 20
    assert s.get("database.pool.timeout") == 5
    assert s.get("database.pool.min_size") == 1


def test_merge_is_recursive_and_non_destructive():
    base = {"a": {"b": {"c": 1, "d": 2}, "e": 3}}
    override = {"a": {"b": {"c": 9}}}
    assert merge(base, override) == {"a": {"b": {"c": 9, "d": 2}, "e": 3}}
    assert base == {"a": {"b": {"c": 1, "d": 2}, "e": 3}}


def test_defaults_untouched_after_nested_override(tmp_path):
    load_settings(write(tmp_path, {"database": {"pool": {"max_size": 99}}}), env={})
    assert DEFAULTS["database"]["pool"] == {
        "min_size": 1, "max_size": 10, "timeout": 30.0, "recycle": 1800,
    }
