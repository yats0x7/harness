from pathlib import Path

from wrench.tools import Toolbox, is_test_path
from wrench.workspace import Workspace


def _box(repo, tmp_path):
    ws = Workspace.prepare(str(repo), tmp_path / "runs", tmp_path / "ws")
    return ws, Toolbox(ws, output_chars=2000, command_timeout=30, test_timeout=60)


def test_exact_edit_and_undo(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    out = tb.call("edit_file", {"path": "calc/ops.py", "old_str": "(len(values) + 1)", "new_str": "len(values)"})
    assert out.startswith("Edited calc/ops.py")
    assert "len(values)\n" in (buggy_repo / "calc/ops.py").read_text()
    assert "calc/ops.py" in tb.state.edited
    tb.call("undo_edit", {"path": "calc/ops.py"})
    assert "(len(values) + 1)" in (buggy_repo / "calc/ops.py").read_text()


def test_fuzzy_edit_fixes_indentation(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    out = tb.call("edit_file", {"path": "calc/ops.py",
                                "old_str": "return sum(values) / (len(values) + 1)",
                                "new_str": "if not values:\n    return 0\nreturn sum(values) / len(values)"})
    assert "ignoring whitespace" not in out or "Edited" in out
    text = (buggy_repo / "calc/ops.py").read_text()
    assert "    if not values:\n        return 0\n    return sum(values) / len(values)" in text


def test_edit_that_breaks_syntax_is_rolled_back(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    before = (buggy_repo / "calc/ops.py").read_text()
    out = tb.call("edit_file", {"path": "calc/ops.py", "old_str": "return sum(values)", "new_str": "return sum(values"})
    assert "rejected" in out and "syntax" in out
    assert (buggy_repo / "calc/ops.py").read_text() == before


def test_missing_old_str_shows_closest_match(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    out = tb.call("edit_file", {"path": "calc/ops.py", "old_str": "return sum(vals) / (len(vals) + 1)", "new_str": "x"})
    assert "not found" in out and "closest text" in out


def test_line_number_prefixes_are_stripped(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    out = tb.call("edit_file", {"path": "calc/ops.py", "old_str": "    2\t    return sum(values) / (len(values) + 1)",
                                "new_str": "    2\t    return sum(values) / len(values)"})
    assert out.startswith("Edited")


def test_dangerous_commands_are_blocked(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    for cmd in ("git push origin main", "sudo rm x", "rm -rf /", "git reset --hard", "vim a.py"):
        assert "blocked" in tb.call("bash", {"command": cmd})


def test_bash_hides_api_key_and_truncates(buggy_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "secret-value")
    ws, tb = _box(buggy_repo, tmp_path)
    assert "secret-value" not in tb.call("bash", {"command": "env"})
    out = tb.call("bash", {"command": "python3 -c \"print('x' * 10000)\""})
    assert "omitted" in out and "saved to" in out


def test_search_and_find(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    assert "calc/ops.py:1:" in tb.call("search", {"pattern": "def mean"})
    assert "tests/test_ops.py" in tb.call("find_files", {"pattern": "test_*.py"})


def test_paths_cannot_escape_the_repo(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    assert "outside the repository" in tb.call("read_file", {"path": "../../etc/passwd"})


def test_test_path_detection():
    assert is_test_path("tests/test_ops.py") and is_test_path("src/foo.test.ts") and is_test_path("pkg/a_test.go")
    assert not is_test_path("src/testing_utils_impl.py".replace("testing_", "t_"))


def test_repeating_an_applied_edit_says_it_is_already_done(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    args = {"path": "calc/ops.py", "old_str": "(len(values) + 1)", "new_str": "len(values)"}
    tb.call("edit_file", args)
    out = tb.call("edit_file", args)
    assert "already applied" in out and not out.startswith("Error")


def test_small_reads_are_widened(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    (buggy_repo / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(300)))
    out = tb.call("read_file", {"path": "big.py", "start_line": 50, "end_line": 55})
    assert "lines 40-139" in out


def test_near_miss_edit_shows_the_differing_lines(buggy_repo, tmp_path):
    ws, tb = _box(buggy_repo, tmp_path)
    out = tb.call("edit_file", {"path": "calc/ops.py", "old_str": "def mean(vals):\n    return sum(values) / (len(values) + 1)",
                                "new_str": "x"})
    assert "your line 1" in out and "use exactly this as old_str" in out
    assert "def mean(values):" in out
