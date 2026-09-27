import json

from trojan.issue import Issue, TaskSpec
from trojan.lessons import load, record, render


def test_lessons_are_redacted_and_filtered_by_task_kind(tmp_path):
    issue = Issue(text="Fix the parser", task=TaskSpec(kind="bugfix"))
    attempt = type("Attempt", (), {
        "reason": "token=secret-value caused verification failure",
        "summary": "",
        "changed_files": ["src/parser.py"],
        "verification": {"repro_before_exit": 1, "repro_after_exit": 1},
    })()
    record(tmp_path / "runs", issue, attempt)
    rows = load(tmp_path / "runs", "bugfix")
    assert rows[0]["reason"] == "token=<redacted> caused verification failure"
    assert not load(tmp_path / "runs", "feature")
    assert "src/parser.py" in render(rows)
    assert json.loads((tmp_path / "runs" / "lessons.jsonl").read_text())["task_kind"] == "bugfix"
