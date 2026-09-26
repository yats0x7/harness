"""Turning whatever the evaluator hands us into an issue: text, a file, or a GitHub URL."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

_GH_ISSUE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)")


@dataclass
class Issue:
    text: str
    title: str = ""
    url: str = ""
    repo_url: str = ""

    @property
    def short(self) -> str:
        first = self.title or self.text.strip().splitlines()[0] if self.text.strip() else "(empty issue)"
        return first[:100]


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "wrench-harness"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_github_issue(url: str) -> Optional[Issue]:
    m = _GH_ISSUE.search(url)
    if not m:
        return None
    owner, repo, _, number = m.groups()
    api = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    repo_url = f"https://github.com/{owner}/{repo}.git"
    try:
        resp = httpx.get(api, headers=_github_headers(), timeout=20)
        resp.raise_for_status()
        data = resp.json()
        comments = []
        if data.get("comments"):
            c = httpx.get(api + "/comments?per_page=15", headers=_github_headers(), timeout=20)
            if c.status_code == 200:
                comments = [f"Comment by {x.get('user', {}).get('login', '?')}:\n{x.get('body') or ''}" for x in c.json()]
    except (httpx.HTTPError, ValueError):
        return Issue(text=f"GitHub issue {url} (could not be fetched; work from the repository).",
                     url=url, repo_url=repo_url)
    title = data.get("title") or ""
    body = data.get("body") or ""
    text = f"# {title}\n\n{body}".strip()
    if comments:
        text += "\n\n" + "\n\n".join(comments)
    return Issue(text=text, title=title, url=url, repo_url=repo_url)


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
    return Issue(text=text, title=title, repo_url=repo_url)
