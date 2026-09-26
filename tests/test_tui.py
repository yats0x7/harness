"""Drive the Textual app headlessly through a full run against the fake model."""
import asyncio

from trojan.config import load_config
from trojan.tui import TrojanApp

from test_agent import happy_path


def test_tui_runs_an_issue_to_a_verified_fix(fake_model, buggy_repo, tmp_path, monkeypatch):
    import trojan.runner as runner
    monkeypatch.setattr(runner, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(runner, "WORKSPACES", tmp_path / "ws")
    fake_model.script = happy_path()
    cfg = load_config()
    cfg.agent.review = False
    app = TrojanApp(cfg, repo=str(buggy_repo), issue_text="mean([2, 4]) returns 2.0 instead of 3.0")

    async def drive():
        async with app.run_test(size=(140, 45)) as pilot:
            for _ in range(100):
                await pilot.pause(0.1)
                if app.llm:
                    break
            assert app.llm is not None
            await pilot.press("ctrl+s")
            for _ in range(300):
                await pilot.pause(0.1)
                if app.last_patch and not app.running:
                    break
            status = str(app.query_one("#status").render())
            evidence = str(app.query_one("#evidence").render())
            return status, evidence

    status, evidence = asyncio.run(drive())
    assert "VERIFIED" in status
    assert "bug proven fixed" in evidence
    assert "len(values)" in app.last_patch
