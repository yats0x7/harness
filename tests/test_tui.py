"""Drive the Textual app headlessly through a full run against the fake model."""
import asyncio

from trojan.config import load_config
from trojan.tui import TrojanApp

from conftest import text_reply, tool_reply
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


def test_follow_up_task_runs_on_the_same_repo_on_top_of_the_first_fix(fake_model, buggy_repo, tmp_path, monkeypatch):
    import trojan.runner as runner
    from conftest import tool_reply
    monkeypatch.setattr(runner, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(runner, "WORKSPACES", tmp_path / "ws")
    guard = {"path": "calc/ops.py", "old_str": "    return sum(values) / len(values)",
             "new_str": "    if not values:\n        return 0.0\n    return sum(values) / len(values)"}
    fake_model.script = happy_path() + [text_reply('{"verdict": "approve", "problems": []}')] + [
        tool_reply(("edit_file", guard)),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "empty input returns 0.0"})),
        text_reply('{"verdict": "approve", "problems": []}'),
    ]
    cfg = load_config()
    cfg.agent.review = True
    app = TrojanApp(cfg, repo=str(buggy_repo), issue_text="mean([2, 4]) returns 2.0 instead of 3.0")

    async def drive():
        async with app.run_test(size=(140, 45)) as pilot:
            for _ in range(100):
                await pilot.pause(0.1)
                if app.llm:
                    break
            await pilot.press("enter")
            for _ in range(300):
                await pilot.pause(0.1)
                if app.last_patch and not app.running:
                    break
            await pilot.pause(0.3)
            assert app.query_one("#followup").display
            first_patch = app.last_patch
            box = app.query_one("#followup-input")
            box.load_text("also return 0.0 for an empty list")
            await pilot.press("enter")
            for _ in range(300):
                await pilot.pause(0.1)
                if app.last_patch and app.last_patch != first_patch and not app.running:
                    break
            return first_patch, app.last_patch

    first, second = asyncio.run(drive())
    assert "len(values)" in first
    assert "if not values" in second and "+ 1)" not in second  # only the new change
    assert any("Follow-up request" in message.get("content", "")
               and "also return 0.0" in message.get("content", "")
               for req in fake_model.requests for message in req["messages"])
    assert "if not values" in (buggy_repo / "calc/ops.py").read_text()
    assert "(len(values) + 1)" not in (buggy_repo / "calc/ops.py").read_text()
