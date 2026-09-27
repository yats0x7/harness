"""End-to-end runs of the agent loop against the scripted fake model."""
import json
from types import SimpleNamespace

from trojan.agent import (Agent, _has_repro_assertion_failure, _has_test_activity, _has_test_failure,
                          failing_tests, _repro_is_independent)
from trojan.config import load_config
from trojan.issue import Issue
from trojan.llm import LLMClient, LLMError, Usage, resolve_endpoint
from trojan.report import _baseline_status, _execution_status, write_report
from trojan.reviewer import review as review_patch
from trojan.workspace import Workspace

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
    fake_model.script = happy_path() + [text_reply('{"verdict": "approve", "problems": []}')]
    agent, ws = _agent(buggy_repo, tmp_path, review=True)
    result = agent.run()
    assert result.status == "verified"
    v = result.best.verification
    assert v["bug_proven"] is True and v["repro_before_exit"] != 0 and v["repro_after_exit"] == 0
    assert "len(values)" in result.best.patch and "repro.py" not in result.best.patch
    report = write_report(result, ISSUE)
    assert "VERIFIED" in report.read_text()
    assert "Run lifecycle" in report.read_text()
    lifecycle = json.loads(report.with_name("summary.json").read_text())["lifecycle"]
    assert lifecycle["baseline"] == "completed"
    assert lifecycle["verification"] == "independent reproduction failed on original code and passed after the change"
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
    assert result.status == "unverified"


def test_harness_rejects_a_fix_whose_repro_still_fails(fake_model, buggy_repo, tmp_path):
    fake_model.script = [
        tool_reply(("write_file", {"path": "$SCRATCH/repro.py", "content": REPRO})),
        tool_reply(("edit_file", {"path": "calc/ops.py", "old_str": "sum(values)", "new_str": "sum(values) * 1"})),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "fixed", "repro_command": "python $SCRATCH/repro.py"})),
        tool_reply(("edit_file", FIX)),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "really fixed", "repro_command": "python $SCRATCH/repro.py"})),
        text_reply('{"verdict": "approve", "problems": []}'),
    ]
    agent, ws = _agent(buggy_repo, tmp_path, review=True)
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
    assert result.status == "unverified"


def test_retries_rate_limits(fake_model, buggy_repo, tmp_path):
    fake_model.errors = [429, 503]
    fake_model.script = happy_path() + [text_reply('{"verdict": "approve", "problems": []}')]
    agent, ws = _agent(buggy_repo, tmp_path, review=True)
    assert agent.run().status == "verified"


def test_reviewer_can_send_the_patch_back(fake_model, buggy_repo, tmp_path):
    script = happy_path()
    script.insert(6, text_reply('{"verdict": "revise", "problems": ["mean([]) now raises ZeroDivisionError"]}'))
    script += [
        tool_reply(("edit_file", {"path": "calc/ops.py", "old_str": "    return sum(values) / len(values)",
                                  "new_str": "    if not values:\n        return 0.0\n    return sum(values) / len(values)"})),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "handles empty input too", "repro_command": "python $SCRATCH/repro.py"})),
        text_reply('{"verdict": "approve", "problems": []}'),
    ]
    fake_model.script = script
    agent, ws = _agent(buggy_repo, tmp_path, review=True, max_review_rounds=2)
    result = agent.run()
    assert result.status == "verified"
    assert "if not values" in result.best.patch


def test_reviewer_failure_does_not_auto_approve_a_patch():
    class UnavailableReviewer:
        def chat(self, *args, **kwargs):
            raise LLMError("offline")

    approved, problems, usage = review_patch(UnavailableReviewer(), "issue", "diff", "evidence")
    assert not approved
    assert "could not complete" in problems[0]
    assert isinstance(usage, Usage)


def test_second_attempt_runs_only_after_an_unverified_first(fake_model, buggy_repo, tmp_path):
    fake_model.script = ([tool_reply(("read_file", {"path": "calc/ops.py"}))] * 6 + happy_path()
                         + [text_reply('{"verdict": "approve", "problems": []}')])
    agent, ws = _agent(buggy_repo, tmp_path, max_steps=6, max_attempts=2, review=True)
    result = agent.run()
    assert [a.status for a in result.attempts] == ["unfinished", "verified"]
    assert result.status == "verified"


def test_new_test_file_as_reproduction_is_checked_against_the_original_code(fake_model, buggy_repo, tmp_path):
    new_test = "from calc import mean\n\n\ndef test_pair():\n    assert mean([2, 4]) == 3.0\n"
    fake_model.script = [
        tool_reply(("write_file", {"path": "tests/test_pair.py", "content": new_test})),
        tool_reply(("edit_file", FIX)),
        tool_reply(("run_tests", {"target": "tests/test_pair.py"})),
        tool_reply(("finish", {"summary": "fixed", "repro_command": "python -m pytest -q tests/test_pair.py"})),
    ]
    agent, ws = _agent(buggy_repo, tmp_path)
    result = agent.run()
    v = result.best.verification
    assert result.status == "unverified"
    assert v["repro_before_exit"] == 1  # a real assertion failure, not "file not found" (exit 4)
    assert v["bug_proven"] is True
    assert v["repro_independent"] is False
    assert v["tests_evidence"] is False


def test_an_edit_after_the_test_run_in_the_same_message_is_not_verified(fake_model, buggy_repo, tmp_path):
    fake_model.script = [
        tool_reply(("run_tests", {}), ("edit_file", FIX)),
        tool_reply(("finish", {"summary": "done"})),
    ]
    agent, ws = _agent(buggy_repo, tmp_path, max_steps=2, max_attempts=1)
    agent.run()
    events = (ws.run_dir / "trajectory.jsonl").read_text()
    assert "edited after your last test run" in events


def test_no_change_finish_needs_proof(fake_model, buggy_repo, tmp_path):
    fake_model.script = [
        tool_reply(("finish", {"summary": "nothing to do"})),
        tool_reply(("finish", {"summary": "really nothing", "repro_command": "python $SCRATCH/missing.py"})),
        tool_reply(("edit_file", FIX)),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "fixed after all"})),
    ]
    agent, ws = _agent(buggy_repo, tmp_path, max_attempts=1)
    result = agent.run()
    assert result.status == "unverified"
    assert "len(values)" in result.best.patch


def test_command_not_found_on_the_original_code_is_not_bug_proof(fake_model, buggy_repo, tmp_path):
    # The tool exists only in the working copy (an uncommitted file), so on the original
    # checkout the command is "not found" (127). That is not evidence of the bug.
    agent, ws = _agent(buggy_repo, tmp_path)
    (buggy_repo / "only_in_fixed.sh").write_text("exit 0\n")
    fake_model.script = [
        tool_reply(("edit_file", FIX)),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "fixed", "repro_command": "bash ./only_in_fixed.sh 2>/dev/null || exit 127"})),
    ]
    result = agent.run()
    v = result.best.verification
    assert v["repro_before_exit"] == 127 and v["bug_proven"] is False


def _verify_direct(agent, ws, repro=""):
    toolbox = SimpleNamespace(state=SimpleNamespace(test_targets=[]))
    try:
        return agent._verify(repro, toolbox)
    finally:
        agent._traj.close()


def test_modified_existing_test_expectation_cannot_verify_a_fix(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (buggy_repo / "tests" / "test_ops.py").write_text(
        "from calc import mean\n\n\ndef test_pair():\n    assert mean([2, 4]) == 2.0\n")
    result = _verify_direct(agent, ws)
    assert result["modified_existing_tests"] == ["tests/test_ops.py"]
    assert result["tests_ran"] and result["tests_after_exit"] == 0
    assert result["tests_evidence"] is False


def test_agent_does_not_accept_a_passing_modified_test_as_a_fix(fake_model, buggy_repo, tmp_path):
    changed_test = "from calc import mean\n\n\ndef test_pair():\n    assert mean([2, 4]) == 2.0\n"
    fake_model.script = [
        tool_reply(("write_file", {"path": "tests/test_ops.py", "content": changed_test})),
        tool_reply(("run_tests", {})),
        tool_reply(("finish", {"summary": "fixed"})),
    ]
    agent, _ws = _agent(buggy_repo, tmp_path)
    result = agent.run()
    assert result.status == "unverified"
    assert result.best.tests_modified == ["tests/test_ops.py"]


def test_modified_tests_need_a_separate_fail_before_pass_after_reproduction(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (buggy_repo / "tests" / "test_ops.py").write_text(
        "from calc import mean\n\n\ndef test_pair():\n    assert mean([2, 4]) == 2.0\n")
    (buggy_repo / "calc" / "ops.py").write_text("def mean(values):\n    return sum(values) / len(values)\n")
    ws.scratch.mkdir(exist_ok=True)
    (ws.scratch / "repro.py").write_text(
        "from calc import mean\nassert mean([2, 4]) == 3.0\n")
    result = _verify_direct(agent, ws, "python $SCRATCH/repro.py")
    assert result["modified_existing_tests"] == ["tests/test_ops.py"]
    assert result["tests_evidence"] is False
    assert result["bug_proven"] is True and result["repro_independent"] is True


def test_scratch_reproduction_cannot_hide_a_test_runner(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (ws.scratch / "delegated.py").write_text(
        "import subprocess\nsubprocess.run(['python', '-m', 'pytest', 'tests/test_ops.py'], check=True)\n")
    assert not _repro_is_independent("python $SCRATCH/delegated.py", ws, ["tests/test_ops.py"])
    agent._traj.close()


def test_scratch_reproduction_cannot_delegate_to_an_unmodified_test_suite(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (ws.scratch / "delegated.py").write_text(
        "import subprocess\nsubprocess.run(['python', '-m', 'pytest'], check=True)\n")
    assert not _repro_is_independent("python $SCRATCH/delegated.py", ws, [])
    agent._traj.close()


def test_scratch_reproduction_cannot_assemble_a_test_runner_name(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (ws.scratch / "delegated.py").write_text(
        "import subprocess\nsubprocess.run(['python', '-m', 'py' + 'test'], check=True)\n")
    assert not _repro_is_independent("python $SCRATCH/delegated.py", ws, [])
    agent._traj.close()


def test_scratch_reproduction_cannot_construct_a_test_runner_from_character_codes(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (ws.scratch / "delegated.py").write_text(
        "import subprocess\nrunner = ''.join(map(chr, [112, 121, 116, 101, 115, 116]))\n"
        "subprocess.run(['python', '-m', runner], check=True)\n")
    assert not _repro_is_independent("python $SCRATCH/delegated.py", ws, [])
    agent._traj.close()


def test_scratch_reproduction_cannot_turn_file_presence_into_an_assertion(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (ws.scratch / "presence.py").write_text(
        "from pathlib import Path\n"
        "try: Path('marker.txt').read_bytes()\n"
        "except FileNotFoundError: raise AssertionError('expected marker')\n")
    assert not _repro_is_independent("python $SCRATCH/presence.py", ws, [])
    agent._traj.close()


def test_scratch_reproduction_cannot_use_os_access_as_file_presence_proof(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (ws.scratch / "presence.py").write_text(
        "import os\nassert os.access('marker.txt', os.F_OK), 'expected marker'\n")
    assert not _repro_is_independent("python $SCRATCH/presence.py", ws, [])
    agent._traj.close()


def test_scratch_reproduction_cannot_use_directory_listing_as_presence_proof(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (ws.scratch / "presence.py").write_text(
        "import os\nassert 'marker.txt' in os.listdir('.'), 'expected marker'\n")
    assert not _repro_is_independent("python $SCRATCH/presence.py", ws, [])
    agent._traj.close()


def test_nonstandard_scratch_script_extension_is_still_inspected(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (ws.scratch / "delegated.pyw").write_text(
        "import subprocess\nsubprocess.run(['python', '-m', 'pytest'], check=True)\n")
    assert not _repro_is_independent("python $SCRATCH/delegated.pyw", ws, [])
    agent._traj.close()


def test_scratch_reproduction_cannot_construct_test_runner_with_chr_plus(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (ws.scratch / "delegated.py").write_text(
        "import subprocess\nrunner = chr(112)+chr(121)+chr(116)+chr(101)+chr(115)+chr(116)\n"
        "subprocess.run(['python', '-m', runner], check=True)\n")
    assert not _repro_is_independent("python $SCRATCH/delegated.py", ws, [])
    agent._traj.close()


def test_repository_presence_check_is_not_bug_proof(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (buggy_repo / "marker.txt").write_text("unrelated addition\n")
    result = _verify_direct(agent, ws, "test -f marker.txt")
    assert result["bug_proven"] is False
    assert result["repro_independent"] is False


def test_reproduction_command_must_fail_with_a_behavior_assertion():
    assert _has_repro_assertion_failure("AssertionError: expected 3 but got 2")
    assert not _has_repro_assertion_failure("ModuleNotFoundError: import failed")
    assert not _has_repro_assertion_failure("FileNotFoundError: marker.txt")


def test_weakened_existing_assertion_cannot_verify_a_fix(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (buggy_repo / "tests" / "test_ops.py").write_text("def test_pair():\n    assert True\n")
    result = _verify_direct(agent, ws)
    assert result["modified_existing_tests"] == ["tests/test_ops.py"]
    assert result["tests_evidence"] is False


def test_deleted_test_cannot_make_an_empty_suite_verified(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (buggy_repo / "tests" / "test_ops.py").unlink()
    result = _verify_direct(agent, ws)
    assert result["modified_existing_tests"] == ["tests/test_ops.py"]
    assert result["tests_evidence"] is False
    assert result["tests_after_exit"] != 0


def test_reproduction_that_passes_before_and_after_is_not_fix_evidence(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    (buggy_repo / "calc" / "ops.py").write_text("def mean(values):\n    return sum(values) / len(values)\n")
    ws.scratch.mkdir(exist_ok=True)
    (ws.scratch / "always_passes.py").write_text("print('ok')\n")
    result = _verify_direct(agent, ws, "python $SCRATCH/always_passes.py")
    assert result["repro_after_exit"] == result["repro_before_exit"] == 0
    assert result["bug_proven"] is False
    assert result["repro_independent"] is False


def test_zero_test_success_is_not_verification_evidence(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    ws.test_command = "python -c 'print(\"no tests ran\")'"
    result = _verify_direct(agent, ws)
    assert result["tests_after_exit"] == result["tests_before_exit"] == 0
    assert result["tests_after_activity"] is False
    assert result["tests_evidence"] is False


def test_collection_errors_are_not_counted_as_executed_failing_tests():
    for output in ("ERROR collecting tests/test_app.py\n1 error in 0.2s\n",
                   "ERROR tests/test_app.py\n1 error in 0.2s\n",
                   "ERROR: found no collectors for tests/test_app.py\n"):
        assert not _has_test_activity(output)
        assert not _has_test_failure(output)
        assert not failing_tests(output)


def test_partial_test_count_drop_is_not_verification_evidence(fake_model, buggy_repo, tmp_path):
    agent, ws = _agent(buggy_repo, tmp_path)
    # Simulate a test-runner configuration change that silently narrows discovery.
    (buggy_repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['tests/test_ops.py']\n")
    (buggy_repo / "tests" / "test_ops.py").write_text(
        "from calc import mean\n\n\ndef test_pair():\n    assert mean([2, 4]) == 3.0\n"
        "\ndef test_another_pair():\n    assert mean([0, 2]) == 1.0\n")
    (buggy_repo / "tests" / "test_extra.py").write_text(
        "from calc import mean\n\n\ndef test_existing_case():\n    assert mean([0]) == 0\n"
        "\ndef test_another_existing_case():\n    assert mean([4, 6]) == 5\n")
    (buggy_repo / "calc" / "ops.py").write_text("def mean(values):\n    return sum(values) / len(values)\n")
    result = _verify_direct(agent, ws)
    assert result["tests_after_exit"] == 0
    assert result["tests_coverage_drop"] is True
    assert result["tests_evidence"] is False


def test_report_does_not_call_an_unrunnable_original_baseline_completed():
    assert _baseline_status({"repro_before_exit": 127}) == "failed_to_run"
    assert _baseline_status({"tests_before_exit": 2, "tests_before_activity": False}) == "failed_to_run"
    assert _baseline_status({"tests_before_exit": 1, "tests_before_activity": True}) == "completed"
    assert _baseline_status({"repro_before_exit": 1, "repro_independent": False}) == "unconfirmed"
    assert _baseline_status({"tests_before_exit": -9, "tests_before_activity": True,
                             "tests_before_timed_out": True}) == "timed_out"
    assert _baseline_status({"repro_before_exit": -9, "repro_independent": True,
                             "repro_before_timed_out": True}) == "timed_out"
    assert _baseline_status({}) == "unavailable"


def test_report_does_not_call_interrupted_execution_completed():
    attempt = SimpleNamespace(reason="finished")
    assert _execution_status(SimpleNamespace(error="", attempts=[attempt])) == "completed"
    assert _execution_status(SimpleNamespace(error="interrupted", attempts=[attempt])) == "interrupted"
    assert _execution_status(SimpleNamespace(error="provider failed", attempts=[])) == "failed"
    assert _execution_status(SimpleNamespace(error="", attempts=[SimpleNamespace(reason="cancelled")])) == "cancelled"
