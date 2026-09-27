"""Runner-level tests, including the bounded independent-worker mode."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from trojan import runner
from trojan.agent import Attempt, RunResult
from trojan.config import load_config
from trojan.issue import Issue
from trojan.llm import Endpoint, Usage
from trojan.workspace import Workspace

from conftest import make_repo


def test_result_score_prefers_verified_before_token_cost():
    verified = RunResult("verified", [Attempt(1, status="verified", patch="x")],
                         Attempt(1, status="verified", patch="x"), Usage(prompt=500), 1,
                         Path("/tmp/a"), "m", "p")
    cheap_unverified = RunResult("unverified", [Attempt(1, status="unverified", patch="x")],
                                 Attempt(1, status="unverified", patch="x"), Usage(), 1,
                                 Path("/tmp/b"), "m", "p")
    assert runner._result_score(verified) > runner._result_score(cheap_unverified)


def test_best_of_runs_are_isolated_and_applies_winner(monkeypatch, tmp_path):
    repo = make_repo(tmp_path / "repo", {"app.py": "VALUE = 'old'\n"})
    cfg = load_config()
    issue = Issue("Build the requested change")
    endpoint = Endpoint("test", "http://127.0.0.1/v1", "test-model", "not-real")
    template = SimpleNamespace(endpoint=endpoint)
    calls = []

    class FakeAgent:
        def __init__(self, _cfg, _llm, ws, _issue, **_kwargs):
            self.ws = ws

        def run(self):
            calls.append(self.ws.root)
            (self.ws.root / "app.py").write_text("VALUE = 'fixed'\n", encoding="utf-8")
            patch = self.ws.diff()
            attempt = Attempt(1, status="verified", patch=patch, summary="fixed")
            return RunResult("verified", [attempt], attempt, Usage(requests=1), 0.01,
                             self.ws.run_dir, "test-model", "test")

    monkeypatch.setattr(runner, "Agent", FakeAgent)
    monkeypatch.setattr(runner, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(runner, "WORKSPACES", tmp_path / "workspaces")
    events = []
    result, report = runner.execute(cfg, str(repo), issue, events.append, llm=template, best_of=2)

    assert result.status == "verified"
    assert result.tournament["workers"] == 2
    assert result.tournament["completed"] == 2
    assert len(set(calls)) == 2
    assert (repo / "app.py").read_text() == "VALUE = 'fixed'\n"
    assert report.exists()
    assert any(event.get("kind") == "worker_done" for event in events)

