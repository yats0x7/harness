"""Small, redacted failure lessons shared between bounded attempts and runs."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List

from .issue import Issue


def _clean(value: object, limit: int = 500) -> str:
    text = re.sub(r"(?i)((?:api[_-]?key|token|password|secret))\s*[=:]\s*\S+", r"\1=<redacted>", str(value))
    return text[:limit]


def _path(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return root / "lessons.jsonl"


def load(root: Path, task_kind: str, limit: int = 5) -> List[Dict]:
    path = _path(root)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("task_kind") == task_kind:
            rows.append(row)
    return rows[-limit:]


def render(rows: List[Dict]) -> str:
    if not rows:
        return "(none recorded)"
    return "\n".join(f"- {r.get('reason') or 'previous attempt was not verified'} "
                      f"(changed: {', '.join(r.get('changed_files') or []) or 'none'})" for r in rows)


def record(root: Path, issue: Issue, attempt) -> None:
    path = _path(root)
    verification = attempt.verification or {}
    row = {
        "time": int(time.time()),
        "task_kind": issue.kind,
        "request": _clean(issue.short, 160),
        "reason": _clean(attempt.reason or attempt.summary or "not verified"),
        "changed_files": list(getattr(attempt, "changed_files", []) or [])[:20],
        "repro_before_exit": verification.get("repro_before_exit"),
        "repro_after_exit": verification.get("repro_after_exit"),
        "tests_after_exit": verification.get("tests_after_exit"),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
