"""Explicit, post-verification GitHub publishing.

The agent never receives these capabilities. Publishing is a separate CLI
operation that requires an explicit flag and operates in a fresh clone.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import httpx

_REPO_URL = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,80}$")


class GitHubPublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubTarget:
    owner: str
    repo: str

    @property
    def api_root(self) -> str:
        return f"https://api.github.com/repos/{self.owner}/{self.repo}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"


@dataclass(frozen=True)
class PullRequest:
    url: str
    branch: str
    repository: str


def parse_target(url: str) -> GitHubTarget:
    match = _REPO_URL.fullmatch((url or "").strip())
    if not match:
        raise GitHubPublishError("publishing requires a public HTTPS GitHub repository URL")
    return GitHubTarget(*match.groups())


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise GitHubPublishError("set GITHUB_TOKEN or GH_TOKEN to publish a pull request")
    return token.strip()


def _headers(token: str) -> dict:
    return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "User-Agent": "trojan-horse"}


def _git_env(token: str) -> dict:
    env = dict(os.environ)
    # The token is passed through Git's in-process config environment, never a
    # URL, command argument, repository remote, trajectory, or model context.
    for name in list(env):
        if name.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_KEY", "_CREDENTIALS")):
            env.pop(name, None)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: bearer {token}",
    })
    return env


def _git(args: list[str], cwd: Path, token: str, timeout: int = 120) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=_git_env(token), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if proc.returncode:
        # Never include a command or environment value that could contain the token.
        raise GitHubPublishError(f"git {' '.join(args[:2])} failed: {proc.stdout[-800:].strip()}")
    return proc.stdout.strip()


def _repo_from_local(path: str) -> Optional[str]:
    try:
        remote = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                                text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return remote.stdout.strip() if remote.returncode == 0 else None


def _safe_branch(branch: Optional[str], task_kind: str) -> str:
    if branch:
        if not _BRANCH.fullmatch(branch) or branch in {"main", "master", "develop", "dev"}:
            raise GitHubPublishError("branch must be a safe non-default Git branch name")
        return branch
    stamp = time.strftime("%Y%m%d-%H%M%S")
    kind = re.sub(r"[^a-z0-9-]+", "-", (task_kind or "task").lower()).strip("-") or "task"
    return f"trojan/{kind}-{stamp}"


def publish_verified_patch(patch: Path, repo_url: str, task_kind: str = "task", title: str = "",
                           body: str = "", branch: Optional[str] = None, fork: bool = False) -> PullRequest:
    """Push a verified patch and open a PR, only when explicitly requested."""
    if not patch.is_file() or not patch.read_text(encoding="utf-8").strip():
        raise GitHubPublishError("the verified run has no patch to publish")
    token = _token()
    target = parse_target(repo_url)
    branch = _safe_branch(branch, task_kind)
    headers = _headers(token)
    base_response = httpx.get(target.api_root, headers=headers, timeout=20)
    if base_response.status_code != 200:
        raise GitHubPublishError(f"could not inspect the GitHub repository (HTTP {base_response.status_code})")
    base = base_response.json().get("default_branch") or "main"

    push_target = target
    head_prefix = ""
    if fork:
        fork_response = httpx.post(target.api_root + "/forks", headers=headers, timeout=30)
        if fork_response.status_code not in (201, 202, 409):
            raise GitHubPublishError(f"could not create the GitHub fork (HTTP {fork_response.status_code})")
        user_response = httpx.get("https://api.github.com/user", headers=headers, timeout=20)
        if user_response.status_code != 200:
            raise GitHubPublishError("could not identify the GitHub account for the fork")
        login = user_response.json().get("login")
        if not login:
            raise GitHubPublishError("GitHub did not return an account name for the fork")
        push_target = GitHubTarget(login, target.repo)
        head_prefix = f"{login}:"

    temp_root = Path(tempfile.mkdtemp(prefix="trojan-publish-"))
    checkout = temp_root / target.repo
    try:
        _git(["clone", "--quiet", "--depth", "1", "--branch", base, push_target.clone_url, str(checkout)],
             temp_root, token, timeout=900)
        _git(["checkout", "-b", branch], checkout, token)
        applied = subprocess.run(["git", "apply", "--index", str(patch)], cwd=checkout, text=True,
                                 capture_output=True, timeout=120)
        if applied.returncode:
            raise GitHubPublishError(f"verified patch did not apply cleanly: {applied.stdout[-800:]}")
        _git(["-c", "user.name=Trojan Horse", "-c", "user.email=trojan-horse@localhost", "commit", "-m",
              title or f"Apply verified {task_kind} task"], checkout, token)
        _git(["push", "origin", branch], checkout, token, timeout=900)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    pr_title = title or f"Verified {task_kind} task"
    pr_body = body or "This pull request was produced by a verified Trojan Horse run."
    response = httpx.post(target.api_root + "/pulls", headers=headers,
                          json={"title": pr_title, "head": head_prefix + branch, "base": base, "body": pr_body},
                          timeout=30)
    if response.status_code not in (201,):
        raise GitHubPublishError(f"branch pushed, but pull request creation failed (HTTP {response.status_code})")
    return PullRequest(response.json().get("html_url", ""), branch, f"{push_target.owner}/{push_target.repo}")
