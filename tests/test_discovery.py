import json

from trojan.discovery import discover_repository, render


def test_discovery_is_read_only_and_returns_bounded_machine_readable_findings(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("# TODO: handle empty input\ndef run():\n    pass\n")
    (tmp_path / "tests" / "test_app.py").write_text("def test_run():\n    assert True\n")
    (tmp_path / "runs" / "old-run").mkdir(parents=True)
    (tmp_path / "runs" / "old-run" / "trajectory.jsonl").write_text(
        '{"task": "secret issue text", "path": "runs/old-run"}\n')
    (tmp_path / "workspace" / "cloned-repo").mkdir(parents=True)
    (tmp_path / "workspace" / "cloned-repo" / "source.py").write_text("# HACK: generated clone\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    report = discover_repository(str(tmp_path), limit=10)
    assert report["files_scanned"] == 2
    assert report["test_files"] == 1
    assert any(f["kind"] == "error" for f in report["findings"])
    assert any(f["kind"] == "test" for f in report["findings"])
    assert json.loads(render(report))["repository"] == str(tmp_path.resolve())
    assert before == {p: p.read_bytes() for p in before}
