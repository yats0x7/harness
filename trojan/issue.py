"""Turning whatever the evaluator hands us into an issue: text, a file, or a GitHub URL."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

import httpx

_GH_ISSUE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)")
_TASK_KINDS = ("bugfix", "feature", "refactor", "test", "docs", "review", "general")
_KIND_ALIASES = {"bug": "bugfix", "fix": "bugfix", "enhancement": "feature", "documentation": "docs",
                 "testing": "test", "code-review": "review"}


@dataclass(frozen=True)
class TaskSpec:
    """Structured intent extracted from a free-form task request."""

    kind: str = "bugfix"
    acceptance: Tuple[str, ...] = ()
    constraints: Tuple[str, ...] = ()


def _normalise_kind(value: str) -> Optional[str]:
    value = value.strip().lower().replace("_", "-")
    value = _KIND_ALIASES.get(value, value)
    return value if value in _TASK_KINDS else None


def _section_items(text: str, headers: Tuple[str, ...]) -> Tuple[str, ...]:
    """Read bullet/numbered items under a small Markdown-style section."""
    lines = text.splitlines()
    wanted = re.compile(r"^\s*(?:#+\s*)?(?:" + "|".join(re.escape(h) for h in headers) + r")\s*:\s*(.*)$", re.I)
    items = []
    active = False
    for line in lines:
        match = wanted.match(line)
        if match:
            active = True
            inline = match.group(1).strip()
            if inline:
                items.append(inline)
            continue
        if active:
            if not line.strip():
                break
            bullet = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.*)$", line)
            if bullet:
                items.append(bullet.group(1).strip())
            elif re.match(r"^\s*#+\s+", line) or re.match(r"^\s*[A-Za-z][\w -]+:\s*", line):
                break
    return tuple(dict.fromkeys(x for x in items if x))


def task_spec(text: str, source_url: str = "") -> TaskSpec:
    """Classify a free-form request while allowing an explicit ``Type:`` line."""
    explicit = re.search(r"^\s*(?:task\s+)?(?:type|kind)\s*:\s*([^\n]+)", text, re.I | re.M)
    kind = _normalise_kind(explicit.group(1)) if explicit else None
    lower = text.lower()
    if kind is None and "/issues/" in source_url:
        kind = "bugfix"
    if kind is None:
        patterns = (
            ("refactor", r"\b(refactor|rewrite|simplif|cleanup|migrat)\b"),
            ("test", r"\b(add|write|improve|increase)\b.{0,30}\btests?\b|\btest coverage\b"),
            ("docs", r"\b(document|documentation|readme|docs?)\b"),
            ("review", r"\b(review|audit|inspect|explain)\b"),
            ("feature", r"\b(build|implement|add|create|introduce|support|enable)\b"),
            ("bugfix", r"\b(bug|fix|broken|error|failing|failure|regression|incorrect|wrong|crash)\b"),
        )
        for candidate, pattern in patterns:
            if re.search(pattern, lower):
                kind = candidate
                break
    return TaskSpec(kind=kind or "bugfix",
                    acceptance=_section_items(text, ("acceptance criteria", "acceptance", "definition of done")),
                    constraints=_section_items(text, ("constraints", "requirements", "non-goals")))


@dataclass
class Issue:
    text: str
    title: str = ""
    url: str = ""
    repo_url: str = ""
    task: TaskSpec = field(default_factory=TaskSpec)

    @property
    def short(self) -> str:
        first = self.title or self.text.strip().splitlines()[0] if self.text.strip() else "(empty issue)"
        return first[:100]

    @property
    def kind(self) -> str:
        return self.task.kind


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "trojan-horse"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class IssueFetchError(RuntimeError):
    pass


def _via_gh_cli(owner: str, repo: str, number: str) -> Optional[dict]:
    """Fetch the issue with the GitHub CLI, which uses the user's own login."""
    import json
    import shutil
    import subprocess

    if not shutil.which("gh"):
        return None
    try:
        out = subprocess.run(["gh", "issue", "view", number, "-R", f"{owner}/{repo}", "--json", "title,body,comments"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    data = json.loads(out.stdout)
    return {"title": data.get("title", ""), "body": data.get("body", ""),
            "comments": [{"user": {"login": (c.get("author") or {}).get("login", "?")}, "body": c.get("body", "")}
                         for c in data.get("comments", [])]}


def fetch_github_issue(url: str) -> Optional[Issue]:
    """Fetch an issue and its comments. Raises IssueFetchError rather than guessing."""
    import time

    m = _GH_ISSUE.search(url)
    if not m:
        return None
    owner, repo, _, number = m.groups()
    api = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    repo_url = f"https://github.com/{owner}/{repo}.git"
    data, comments, last = None, [], ""
    for attempt in range(3):
        try:
            resp = httpx.get(api, headers=_github_headers(), timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("comments"):
                    c = httpx.get(api + "/comments?per_page=15", headers=_github_headers(), timeout=20)
                    comments = c.json() if c.status_code == 200 else []
                break
            last = f"HTTP {resp.status_code}"
            if resp.status_code == 404:
                break
        except (httpx.HTTPError, ValueError) as exc:
            last = type(exc).__name__
        time.sleep(2 * (attempt + 1))
    if data is None:
        data = _via_gh_cli(owner, repo, number)
        comments = (data or {}).get("comments", [])
    if data is None:
        hint = " GitHub's anonymous rate limit may be used up; set GITHUB_TOKEN," if last in ("HTTP 403", "HTTP 429") else ""
        raise IssueFetchError(f"Could not fetch {url} ({last}).{hint} or paste the issue text instead.")
    title = data.get("title") or ""
    text = f"# {title}\n\n{data.get('body') or ''}".strip()
    notes = [f"Comment by {(x.get('user') or {}).get('login', '?')}:\n{x.get('body') or ''}" for x in comments
             if "github-actions" not in str((x.get("user") or {}).get("login", ""))]
    if notes:
        text += "\n\n" + "\n\n".join(notes)
    return Issue(text=text, title=title, url=url, repo_url=repo_url, task=task_spec(text, url))


def load_issue(value: str) -> Issue:
    """Accept issue text, @path, a path to a file, a GitHub issue URL, or '-' for stdin."""
    value = (value or "").strip()
    if value == "-":
        import sys
        value = sys.stdin.read()
    elif value.startswith("@"):
        value = Path(value[1:]).expanduser().read_text()
    elif len(value) < 1024 and "\n" not in value and Path(value).expanduser().is_file():
        value = Path(value).expanduser().read_text()
    if _GH_ISSUE.fullmatch(value.strip()):
        issue = fetch_github_issue(value.strip())
        if issue:
            return issue
    m = _GH_ISSUE.search(value)
    text = value.strip()
    title = text.splitlines()[0].lstrip("# ").strip() if text else ""
    repo_url = ""
    if m:
        repo_url = f"https://github.com/{m.group(1)}/{m.group(2)}.git"
    return Issue(text=text, title=title, repo_url=repo_url, task=task_spec(text))
