"""Glue shared by the CLI and the TUI: resolve the model, prepare the repo, run, report."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from .agent import Agent, RunResult
from .config import ROOT, Config
from .issue import Issue
from .llm import Endpoint, LLMClient, resolve_endpoint
from .report import write_report
from .workspace import Workspace

RUNS = ROOT / "runs"
WORKSPACES = ROOT / "workspace"

Event = Dict[str, Any]


def connect(cfg: Config) -> LLMClient:
    return LLMClient(resolve_endpoint(cfg), cfg.model)


def execute(cfg: Config, repo: str, issue: Issue, on_event: Callable[[Event], None],
            cancel: Optional[threading.Event] = None, llm: Optional[LLMClient] = None) -> Tuple[RunResult, Path]:
    on_event({"t": 0, "kind": "status", "text": "Connecting to the model"})
    llm = llm or connect(cfg)
    on_event({"t": 0, "kind": "status", "text": f"Using {llm.endpoint.model} via {llm.endpoint.provider}"})
    on_event({"t": 0, "kind": "status", "text": "Preparing the repository"})
    ws = Workspace.prepare(repo, RUNS, WORKSPACES)
    (ws.run_dir / "issue.md").write_text(issue.text, encoding="utf-8")
    agent = Agent(cfg, llm, ws, issue, on_event=on_event, cancel=cancel)
    result = agent.run()
    report = write_report(result, issue)
    on_event({"t": round(result.elapsed, 2), "kind": "report", "path": str(report), "run_dir": str(ws.run_dir)})
    return result, report


def replay(run_dir: Path, speed: float = 8.0) -> Iterator[Event]:
    """Yield a finished run's events with their original pacing, sped up."""
    path = Path(run_dir) / "trajectory.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no trajectory.jsonl in {run_dir}")
    last = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        gap = max(0.0, float(event.get("t", 0)) - last)
        last = float(event.get("t", 0))
        time.sleep(min(gap / speed, 1.5))
        yield event


def latest_run() -> Optional[Path]:
    """The newest local run, or the recorded example run shipped in examples/."""
    for base in (RUNS, ROOT / "examples"):
        if base.exists():
            runs = sorted((p for p in base.iterdir() if (p / "trajectory.jsonl").exists()), key=lambda p: p.name)
            if runs:
                return runs[-1]
    return None
