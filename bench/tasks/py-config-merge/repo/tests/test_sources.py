from layercfg.merge import merge, merge_all
from layercfg.sources import coerce, read_env


def test_coerce():
    assert coerce("42") == 42
    assert coerce("0.5") == 0.5
    assert coerce("true") is True
    assert coerce("Off") is False
    assert coerce("none") is None
    assert coerce("hello") == "hello"


def test_read_env_nests_and_filters():
    env = {"APP__SERVER__PORT": "9000", "APP__DATABASE__POOL__MAX_SIZE": "4", "HOME": "/root"}
    assert read_env(env) == {"server": {"port": 9000}, "database": {"pool": {"max_size": 4}}}


def test_merge_is_non_destructive():
    base = {"a": {"x": 1, "y": 2}, "b": 1}
    override = {"a": {"y": 3}, "c": 4}
    out = merge(base, override)
    assert out == {"a": {"x": 1, "y": 3}, "b": 1, "c": 4}
    assert base == {"a": {"x": 1, "y": 2}, "b": 1}
    assert override == {"a": {"y": 3}, "c": 4}


def test_merge_replaces_non_dict_with_dict():
    assert merge({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}


def test_merge_all_skips_empty_layers():
    assert merge_all([{"a": 1}, {}, None, {"b": 2}]) == {"a": 1, "b": 2}
