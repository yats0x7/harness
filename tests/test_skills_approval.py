"""Skills anyone can add, and the ask / auto-approve modes."""
import asyncio

from trojan.skills import create, discover, listing
from trojan.tools import Toolbox
from trojan.workspace import Workspace

from conftest import text_reply, tool_reply
from test_agent import FIX, ISSUE, _agent, happy_path


def _box(repo, tmp_path, **kw):
    ws = Workspace.prepare(str(repo), tmp_path / "runs", tmp_path / "ws")
    return ws, Toolbox(ws, 2000, 30, 60, **kw)


def test_user_and_repo_skills_are_discovered_and_repo_wins(buggy_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("TROJAN_SKILLS_HOME", str(tmp_path / "home-skills"))
    import importlib, trojan.skills as sk
    importlib.reload(sk)
    path = sk.create("my-rules", tmp_path / "home-skills")
    assert path.read_text().startswith("---\nname: my-rules")
    repo_skill = buggy_repo / ".trojan" / "skills" / "my-rules"
    repo_skill.mkdir(parents=True)
    (repo_skill / "SKILL.md").write_text("---\nname: my-rules\ndescription: repo version\n---\nUse tabs.\n")
    found = sk.discover(buggy_repo)
    assert {"python-pytest", "node-testing", "async-race-bugs", "my-rules"} <= set(found)
    assert found["my-rules"].source == "repo" and "repo version" in sk.listing(found)


def test_use_skill_returns_the_instructions(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path, skills=discover())
    out = tb.call("use_skill", {"name": "async-race-bugs"})
    assert "gate" in out and "Never rely on sleeps" in out
    assert "no skill named" in tb.call("use_skill", {"name": "nope"})


def test_skills_are_listed_in_the_task(fake_model, buggy_repo, tmp_path):
    fake_model.script = happy_path()
    agent, ws = _agent(buggy_repo, tmp_path)
    agent.run()
    first_prompt = fake_model.requests[0]["messages"][1]["content"]
    assert "use_skill" in first_prompt and "python-pytest" in first_prompt


def test_rejected_edit_changes_nothing_and_tells_the_model(buggy_repo, tmp_path):
    seen = []

    def approver(name, args, preview):
        seen.append((name, preview))
        return False, "use a guard clause instead"

    ws, tb = _box(buggy_repo, tmp_path, approver=approver)
    before = (buggy_repo / "calc/ops.py").read_text()
    out = tb.call("edit_file", FIX)
    assert (buggy_repo / "calc/ops.py").read_text() == before
    assert "rejected" in out and "guard clause" in out
    assert seen[0][0] == "edit_file" and "-" in seen[0][1] and "+" in seen[0][1]
    assert tb.call("read_file", {"path": "calc/ops.py"}).startswith("calc/ops.py")  # reads never ask
    assert len(seen) == 1


def test_ask_mode_run_with_one_rejection(fake_model, buggy_repo, tmp_path):
    answers = iter([(False, "not that command")] + [(True, "")] * 20)
    fake_model.script = ([tool_reply(("bash", {"command": "rm -f calc/__init__.py"}))] + happy_path()
                         + [text_reply('{"verdict": "approve", "problems": []}')])
    agent, ws = _agent(buggy_repo, tmp_path, review=True)
    agent.approver = lambda n, a, p: next(answers)
    result = agent.run()
    assert (buggy_repo / "calc/__init__.py").exists()
    assert result.status == "verified"


def test_tui_ask_mode_pops_up_and_approve_all_finishes(fake_model, buggy_repo, tmp_path, monkeypatch):
    import trojan.runner as runner
    from trojan.config import load_config
    from trojan.tui import ApprovalScreen, TrojanApp
    monkeypatch.setattr(runner, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(runner, "WORKSPACES", tmp_path / "ws")
    fake_model.script = happy_path()
    cfg = load_config()
    cfg.agent.review = False
    cfg.agent.approval = "ask"
    app = TrojanApp(cfg, repo=str(buggy_repo), issue_text="mean([2, 4]) returns 2.0 instead of 3.0")

    async def drive():
        async with app.run_test(size=(140, 45)) as pilot:
            for _ in range(100):
                await pilot.pause(0.1)
                if app.llm:
                    break
            await pilot.press("enter")
            popped = False
            for _ in range(300):
                await pilot.pause(0.1)
                if isinstance(app.screen, ApprovalScreen):
                    popped = True
                    await pilot.press("a")
                if app.last_patch and not app.running:
                    break
            return popped

    assert asyncio.run(drive()) is True
    assert app.approval_mode == "auto" and "len(values)" in app.last_patch
