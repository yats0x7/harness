"""Glue shared by the CLI and the TUI: resolve the model, prepare the repo, run, report."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from .agent import Agent, RunResult
from .config import ROOT, Config
from .issue import Issue
from .llm import LLMClient, Usage, resolve_endpoint
from .report import write_report
from .workspace import SKIP_DIRS, Workspace

RUNS = ROOT / "runs"
WORKSPACES = ROOT / "workspace"

Event = Dict[str, Any]


def connect(cfg: Config) -> LLMClient:
    return LLMClient(resolve_endpoint(cfg), cfg.model)


def _save_session(ws: Workspace, issue: Issue) -> None:
    (ws.run_dir / "session.json").write_text(json.dumps({
        "repo": str(ws.root), "issue": issue.text, "title": issue.title,
        "task_type": issue.kind,
    }, indent=2), encoding="utf-8")


def load_session(run_dir: Path) -> Dict[str, Any]:
    """Load a run's restart data, with a fallback for older reports."""
    run_dir = Path(run_dir)
    session = run_dir / "session.json"
    if session.exists():
        data = json.loads(session.read_text(encoding="utf-8"))
        if data.get("issue") and data.get("repo"):
            return data
    issue_path = run_dir / "issue.md"
    if not issue_path.exists():
        raise FileNotFoundError(f"no resumable session in {run_dir}")
    repo = ""
    trajectory = run_dir / "trajectory.jsonl"
    if trajectory.exists():
        for line in trajectory.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") == "start":
                repo = str(event.get("repo") or "")
                break
    if not repo:
        raise FileNotFoundError(f"the saved run does not record a repository: {run_dir}")
    return {"repo": repo, "issue": issue_path.read_text(encoding="utf-8"), "title": ""}


def _result_score(result: RunResult) -> tuple:
    """Rank a completed worker without allowing a cheap unverified patch to win."""
    best = result.best
    return (1 if result.status == "verified" else 0,
            best.score if best else (0, 0, -999999, 0),
            -result.usage.total)


def _copy_repository(source: Path, destination: Path) -> None:
    """Make an independent worker checkout, including the current dirty tree."""
    ignored_names = (set(SKIP_DIRS) - {".git"}) | {"runs", "workspace"}

    def ignore(_directory: str, names):
        return {name for name in names if name in ignored_names or name == ".npmrc" or name.startswith(".env.")
                or name == ".env"}

    # A linked worktree has a .git *file* pointing back to the source's admin
    # directory. Clone that repository first so workers cannot share its index.
    if (source / ".git").is_file():
        result = subprocess.run(
            ["git", "clone", "--quiet", "--no-local", str(source), str(destination)],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"could not clone tournament worker: {result.stderr[-1000:]}")
        shutil.copytree(source, destination, symlinks=True, ignore=ignore, dirs_exist_ok=True)
    else:
        shutil.copytree(source, destination, symlinks=True, ignore=ignore)
    # Dependencies can be large and are deliberately not copied, but tests still
    # need them. Read-only links preserve the normal repository environment.
    for dependency in ("node_modules", ".venv", "venv", "vendor"):
        source_dependency = source / dependency
        destination_dependency = destination / dependency
        if source_dependency.is_dir() and not destination_dependency.exists():
            try:
                destination_dependency.symlink_to(source_dependency, target_is_directory=True)
            except OSError:
                pass


def _tournament_execute(cfg: Config, repo: str, issue: Issue, on_event: Callable[[Event], None],
                        workers: int, cancel: Optional[threading.Event], template_llm: LLMClient,
                        approver=None) -> Tuple[RunResult, Path]:
    if approver is not None:
        raise RuntimeError("--best-of requires automatic approval; use --approval auto")

    started = time.time()
    ws = Workspace.prepare(repo, RUNS, WORKSPACES)
    (ws.run_dir / "issue.md").write_text(issue.text, encoding="utf-8")
    _save_session(ws, issue)
    temp_root = Path(tempfile.mkdtemp(prefix="trojan-tournament-"))
    event_lock = threading.Lock()
    worker_specs = []
    try:
        for number in range(1, workers + 1):
            worker_root = temp_root / f"worker-{number}"
            _copy_repository(ws.root, worker_root)
            worker_ws = Workspace.prepare(str(worker_root), temp_root / "runs", temp_root / "workspace")
            (worker_ws.run_dir / "issue.md").write_text(issue.text, encoding="utf-8")
            worker_specs.append((number, worker_ws))

        on_event({"t": 0, "kind": "status", "text": f"Running {workers} independent workers"})

        def run_worker(number: int, worker_ws: Workspace):
            endpoint = template_llm.endpoint
            worker_llm = LLMClient(endpoint, cfg.model)

            def worker_event(event: Event) -> None:
                enriched = dict(event)
                enriched["worker"] = number
                enriched["tournament"] = True
                with event_lock:
                    on_event(enriched)

            agent = Agent(cfg, worker_llm, worker_ws, issue, on_event=worker_event, cancel=cancel)
            return number, worker_ws, agent.run()

        completed = []
        failures = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="trojan-worker") as pool:
            futures = [pool.submit(run_worker, number, worker_ws) for number, worker_ws in worker_specs]
            for future in as_completed(futures):
                try:
                    item = future.result()
                    completed.append(item)
                    number, _worker_ws, result = item
                    on_event({"t": round(time.time() - started, 2), "kind": "worker_done",
                              "worker": number, "status": result.status,
                              "score": list(_result_score(result))})
                except Exception as exc:
                    failures.append(exc)
                    on_event({"t": round(time.time() - started, 2), "kind": "worker_error",
                              "text": str(exc)})

        if not completed:
            detail = str(failures[0]) if failures else "all workers stopped without a result"
            raise RuntimeError(f"tournament failed: {detail}")

        winner_number, _winner_ws, winner = max(completed, key=lambda item: _result_score(item[2]))
        ws.reset_to_base()
        if winner.best and not ws.apply_patch(winner.best.patch):
            raise RuntimeError("the winning worker produced a patch that could not be applied")

        usage = Usage()
        for _number, _worker_ws, result in completed:
            usage.add(result.usage)
        costs = [result.cost for _number, _worker_ws, result in completed]
        cost = sum(costs) if costs and all(value is not None for value in costs) else None
        tournament = {
            "workers": workers,
            "completed": len(completed),
            "winner": winner_number,
            "results": [{"worker": number, "status": result.status,
                          "score": list(_result_score(result)), "requests": result.usage.requests,
                          "cost_usd": result.cost}
                         for number, _worker_ws, result in sorted(completed)],
        }
        if failures:
            tournament["worker_errors"] = [str(exc) for exc in failures]
        result = RunResult(status=winner.status, attempts=winner.attempts, best=winner.best,
                           usage=usage, elapsed=time.time() - started, run_dir=ws.run_dir,
                           model=winner.model, provider=winner.provider, cost=cost,
                           error=winner.error, tournament=tournament)
        report = write_report(result, issue)
        on_event({"t": round(result.elapsed, 2), "kind": "report", "path": str(report),
                  "run_dir": str(ws.run_dir), "winner": winner_number})
        return result, report
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def execute(cfg: Config, repo: str, issue: Issue, on_event: Callable[[Event], None],
            cancel: Optional[threading.Event] = None, llm: Optional[LLMClient] = None,
            approver=None, best_of: int = 1) -> Tuple[RunResult, Path]:
    if best_of < 1 or best_of > 4:
        raise ValueError("best_of must be between 1 and 4")
    on_event({"t": 0, "kind": "status", "text": "Connecting to the model"})
    llm = llm or connect(cfg)
    on_event({"t": 0, "kind": "status", "text": f"Using {llm.endpoint.model} via {llm.endpoint.provider}"})
    if best_of > 1:
        on_event({"t": 0, "kind": "status", "text": "Preparing independent tournament workers"})
        return _tournament_execute(cfg, repo, issue, on_event, best_of, cancel, llm, approver)
    on_event({"t": 0, "kind": "status", "text": "Preparing the repository"})
    ws = Workspace.prepare(repo, RUNS, WORKSPACES)
    (ws.run_dir / "issue.md").write_text(issue.text, encoding="utf-8")
    _save_session(ws, issue)
    agent = Agent(cfg, llm, ws, issue, on_event=on_event, cancel=cancel, approver=approver)
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


def latest_resumable_run() -> Optional[Path]:
    """The newest actual run, excluding the read-only example replay."""
    if not RUNS.exists():
        return None
    runs = sorted((p for p in RUNS.iterdir() if (p / "issue.md").exists()), key=lambda p: p.name)
    return runs[-1] if runs else None
