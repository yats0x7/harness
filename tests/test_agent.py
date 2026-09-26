"""End-to-end runs of the agent loop against the scripted fake model."""
from wrench.agent import Agent
from wrench.config import load_config
from wrench.issue import Issue
from wrench.llm import LLMClient, resolve_endpoint
from wrench.report import write_report
from wrench.workspace import Workspace

from conftest import text_reply, tool_reply

ISSUE = Issue(text="# mean() is wrong\n\n`mean([2, 4])` returns 2.0 instead of 3.0.")
REPRO = "import sys\nfrom calc import mean\nassert mean([2, 4]) == 3.0, mean([2, 4])\n"
FIX = {"path": "calc/ops.py", "old_str": "(len(values) + 1)", "new_str": "len(values)"}


def _agent(repo, tmp_path, **agent_overrides):
    cfg = load_config()
    cfg.agent.review = agent_overrides.pop("review", False)
    for k, v in agent_overrides.items():
        setattr(cfg.agent, k, v)
    ws = Workspace.prepare(str(repo), tmp_path / "runs", tmp_path / "ws")
    llm = LLMClient(resolve_endpoint(cfg), cfg.model)
    llm._sleep = lambda *a, **k: None
    return Agent(cfg, llm, ws, ISSUE), ws


def happy_path():
    return [
        tool_reply(("read_file", {"path": "calc/ops.py"}), reasoning="look at the code"),
        tool_reply(("write_file", {"path": "$SCRATCH/repro.py", "content": REPRO})),
        tool_reply(("bash", {"command": "python $SCRATCH/repro.py"})),
        tool_reply(("edit_file", FIX)),
        tool_reply(("bash", {"command": "python $SCRATCH/repro.py"}), ("run_tests", {})),
        tool_reply(("finish", {"summary": "divide by len(values)", "repro_command": "python $SCRATCH/repro.py"})),
    ]


def test_happy_path_is_verified_with_bug_proof(fake_model, buggy_repo, tmp_path):
    fake_model.script = happy_path()
    agent, ws = _agent(buggy_repo, tmp_path)
    result = agent.run()
    assert result.status == "verified"
    v = result.best.verification
    assert v["bug_proven"] is True and v["repro_before_exit"] != 0 and v["repro_after_exit"] == 0
    assert "len(values)" in result.best.patch and "repro.py" not in result.best.patch
    report = write_report(result, ISSUE)
    assert "VERIFIED" in report.read_text()
    # DeepSeek needs its reasoning echoed back inside the tool loop.
    later = fake_model.requests[1]["messages"]
    assert any(m.get("reasoning_content") == "look at the code" for m in later)
    assert result.usage.cached > 0


def test_finish_is_rejected_when_edits_are_untested(fake_model, buggy_repo, tmp_path):
    fake_model.script = [
        tool_reply(("edit_file", FIX)),
        tool_reply(("finish", {"summary": "done"})),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "done, tests pass"})),
    ]
    agent, ws = _agent(buggy_repo, tmp_path)
    result = agent.run()
    tool_msgs = [m for m in fake_model.requests[2]["messages"] if m["role"] == "tool"]
    assert "edited after your last test run" in tool_msgs[-1]["content"]
    assert result.status == "verified"


def test_harness_rejects_a_fix_whose_repro_still_fails(fake_model, buggy_repo, tmp_path):
    fake_model.script = [
        tool_reply(("write_file", {"path": "$SCRATCH/repro.py", "content": REPRO})),
        tool_reply(("edit_file", {"path": "calc/ops.py", "old_str": "sum(values)", "new_str": "sum(values) * 1"})),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "fixed", "repro_command": "python $SCRATCH/repro.py"})),
        tool_reply(("edit_file", FIX)),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "really fixed", "repro_command": "python $SCRATCH/repro.py"})),
    ]
    agent, ws = _agent(buggy_repo, tmp_path)
    result = agent.run()
    assert result.status == "verified"
    assert any("still fails" in m.get("content", "") for m in fake_model.requests[4]["messages"] if m["role"] == "tool")


def test_tool_calls_leaked_as_text_are_recovered(fake_model, buggy_repo, tmp_path):
    fake_model.script = [
        text_reply("<tool_call>\n<function=edit_file>\n<parameter=path>\ncalc/ops.py\n</parameter>\n"
                   "<parameter=old_str>\n(len(values) + 1)\n</parameter>\n<parameter=new_str>\nlen(values)\n"
                   "</parameter>\n</function>\n</tool_call>"),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "fixed"})),
    ]
    agent, ws = _agent(buggy_repo, tmp_path)
    result = agent.run()
    assert "len(values)" in result.best.patch
    assert result.status == "verified"


def test_retries_rate_limits(fake_model, buggy_repo, tmp_path):
    fake_model.errors = [429, 503]
    fake_model.script = happy_path()
    agent, ws = _agent(buggy_repo, tmp_path)
    assert agent.run().status == "verified"


def test_reviewer_can_send_the_patch_back(fake_model, buggy_repo, tmp_path):
    script = happy_path()
    script.insert(6, text_reply('{"verdict": "revise", "problems": ["mean([]) now raises ZeroDivisionError"]}'))
    script += [
        tool_reply(("edit_file", {"path": "calc/ops.py", "old_str": "    return sum(values) / len(values)",
                                  "new_str": "    if not values:\n        return 0.0\n    return sum(values) / len(values)"})),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "handles empty input too", "repro_command": "python $SCRATCH/repro.py"})),
    ]
    fake_model.script = script
    agent, ws = _agent(buggy_repo, tmp_path, review=True, max_review_rounds=1)
    result = agent.run()
    assert result.status == "verified"
    assert "if not values" in result.best.patch


def test_second_attempt_runs_only_after_an_unverified_first(fake_model, buggy_repo, tmp_path):
    fake_model.script = [tool_reply(("read_file", {"path": "calc/ops.py"}))] * 6 + happy_path()
    agent, ws = _agent(buggy_repo, tmp_path, max_steps=6, max_attempts=2)
    result = agent.run()
    assert [a.status for a in result.attempts] == ["unfinished", "verified"]
    assert result.status == "verified"
