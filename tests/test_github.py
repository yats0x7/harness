import sys

import pytest

from trojan.github import GitHubPublishError, _git_env, _safe_branch, _verify_fresh_checkout, parse_target


def test_github_target_requires_plain_https_repository_url():
    target = parse_target("https://github.com/example/project.git")
    assert (target.owner, target.repo) == ("example", "project")
    with pytest.raises(GitHubPublishError):
        parse_target("https://user:secret@github.com/example/project.git")
    with pytest.raises(GitHubPublishError):
        parse_target("git@github.com:example/project.git")


def test_publish_branch_never_defaults_to_a_protected_branch():
    assert _safe_branch(None, "feature").startswith("trojan/feature-")
    with pytest.raises(GitHubPublishError):
        _safe_branch("main", "feature")
    with pytest.raises(GitHubPublishError):
        _safe_branch("bad branch", "feature")


def test_git_publish_environment_hides_other_operator_secrets(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "model-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    env = _git_env("github-secret")
    assert env["GIT_CONFIG_VALUE_0"] == "AUTHORIZATION: bearer github-secret"
    assert "AI_API_KEY" not in env and "GITHUB_TOKEN" not in env


def test_publish_rechecks_verified_commands_against_the_fresh_base(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _verify_fresh_checkout(repo, {"tests_command": f"{sys.executable} -c 'assert True'"}, tmp_path)
    with pytest.raises(GitHubPublishError, match="fresh-base test suite failed"):
        _verify_fresh_checkout(repo, {"tests_command": f"{sys.executable} -c 'assert False'"}, tmp_path)
    with pytest.raises(GitHubPublishError, match="recorded reproduction or test command"):
        _verify_fresh_checkout(repo, {}, tmp_path)
