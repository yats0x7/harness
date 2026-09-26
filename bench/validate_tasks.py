#!/usr/bin/env python3
"""Check that every benchmark task is well formed.

For each task under bench/tasks/<id>/ this script:

  1. copies repo/ into a temporary directory and makes it a git repo
  2. runs the visible tests                      -> must pass
  3. copies the hidden test into the repo root and runs it -> must fail
  4. applies solution.patch with `git apply`     -> must apply cleanly
  5. reruns the hidden test                      -> must pass
  6. reruns the visible tests                    -> must pass

It prints a table and exits non-zero if any check fails.

Commands in meta.json that start with "python" or "python3" are run with the
interpreter given by --python (default: the one running this script), so the
checks do not depend on what `python` means on PATH. That interpreter needs
pytest installed.

Usage:
    python3 bench/validate_tasks.py [--task ID ...] [--python PATH] [--keep] [-v]
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_DIR = os.path.join(BENCH_DIR, "tasks")
REQUIRED_FILES = ("issue.md", "solution.patch", "meta.json")
REQUIRED_META = ("id", "language", "difficulty", "hidden_test_cmd", "visible_test_cmd", "description")
COMMAND_TIMEOUT = 300
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", "node_modules", ".git")

GIT = [
    "git",
    "-c", "user.name=bench-validator",
    "-c", "user.email=bench-validator@localhost",
    "-c", "commit.gpgsign=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "init.defaultBranch=main",
]


class StepResult(object):
    def __init__(self, ok, label, output=""):
        self.ok = ok
        self.label = label
        self.output = output


def resolve_command(cmd, python):
    argv = shlex.split(cmd)
    if argv and argv[0] in ("python", "python3"):
        argv[0] = python
    return argv


def run(argv, cwd):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPATH", None)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=COMMAND_TIMEOUT,
        )
    except FileNotFoundError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired as exc:
        out = exc.output or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out + "\n[timed out after %ss]" % COMMAND_TIMEOUT
    return proc.returncode, proc.stdout


def expect_pass(argv, cwd):
    code, out = run(argv, cwd)
    if code == 0:
        return StepResult(True, "pass", out)
    return StepResult(False, "FAIL (exit %d)" % code, out)


def expect_fail(argv, cwd):
    """The hidden test must fail because tests fail, not because it cannot run."""
    code, out = run(argv, cwd)
    if code == 0:
        return StepResult(False, "PASSED (should fail)", out)
    is_pytest = "pytest" in argv
    if is_pytest and code != 1:
        # pytest: 2 = interrupted/collection error, 4 = usage error, 5 = no tests
        return StepResult(False, "ERROR (exit %d)" % code, out)
    if code in (124, 127):
        return StepResult(False, "ERROR (exit %d)" % code, out)
    return StepResult(True, "fails", out)


def hidden_test_file(task_dir):
    for name in ("hidden_test.py", "hidden_test.mjs", "hidden_test.js"):
        if os.path.isfile(os.path.join(task_dir, name)):
            return name
    return None


def check_structure(task_dir):
    problems = []
    if not os.path.isdir(os.path.join(task_dir, "repo")):
        problems.append("missing repo/")
    for name in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(task_dir, name)):
            problems.append("missing %s" % name)
    if hidden_test_file(task_dir) is None:
        problems.append("missing hidden_test.py/.mjs")
    meta = {}
    meta_path = os.path.join(task_dir, "meta.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path) as fh:
                meta = json.load(fh)
        except ValueError as exc:
            problems.append("meta.json is not valid JSON: %s" % exc)
        missing = [k for k in REQUIRED_META if k not in meta]
        if missing:
            problems.append("meta.json missing keys: %s" % ", ".join(missing))
        if meta.get("id") not in (None, os.path.basename(task_dir)):
            problems.append("meta.json id %r does not match directory name" % meta.get("id"))
    return meta, problems


def validate_task(task_dir, python, keep, verbose):
    task_id = os.path.basename(task_dir)
    row = {"task": task_id, "lang": "?", "difficulty": "?"}
    steps = ["visible_before", "hidden_before", "apply_patch", "hidden_after", "visible_after"]
    for s in steps:
        row[s] = "-"

    meta, problems = check_structure(task_dir)
    row["lang"] = meta.get("language", "?")
    row["difficulty"] = meta.get("difficulty", "?")
    if problems:
        row["visible_before"] = "BAD TASK"
        return row, False, ["%s: %s" % (task_id, p) for p in problems]

    visible = resolve_command(meta["visible_test_cmd"], python)
    hidden = resolve_command(meta["hidden_test_cmd"], python)
    hidden_name = hidden_test_file(task_dir)
    patch_path = os.path.join(task_dir, "solution.patch")

    tmp_root = tempfile.mkdtemp(prefix="bench-%s-" % task_id)
    work = os.path.join(tmp_root, "repo")
    details = []
    ok = True
    try:
        shutil.copytree(os.path.join(task_dir, "repo"), work, ignore=IGNORE)
        for argv in (GIT + ["init", "-q"], GIT + ["add", "-A"], GIT + ["commit", "-q", "-m", "task base"]):
            code, out = run(argv, work)
            if code != 0:
                row["visible_before"] = "GIT ERROR"
                return row, False, ["%s: %s failed:\n%s" % (task_id, " ".join(argv[-3:]), out)]

        def record(step, result, argv):
            row[step] = result.label
            if not result.ok:
                details.append("%s / %s: %s\n$ %s\n%s" % (
                    task_id, step, result.label, " ".join(argv), tail(result.output)))
            elif verbose:
                details.append("%s / %s: ok\n%s" % (task_id, step, tail(result.output, 10)))
            return result.ok

        ok &= record("visible_before", expect_pass(visible, work), visible)

        shutil.copy2(os.path.join(task_dir, hidden_name), os.path.join(work, hidden_name))
        ok &= record("hidden_before", expect_fail(hidden, work), hidden)

        apply_argv = GIT + ["apply", "--whitespace=nowarn", patch_path]
        code, out = run(apply_argv, work)
        applied = StepResult(code == 0, "applied" if code == 0 else "FAIL (exit %d)" % code, out)
        ok &= record("apply_patch", applied, ["git", "apply", patch_path])
        if not applied.ok:
            return row, False, details

        ok &= record("hidden_after", expect_pass(hidden, work), hidden)
        ok &= record("visible_after", expect_pass(visible, work), visible)
    finally:
        if keep:
            details.append("%s: kept working copy at %s" % (task_id, work))
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)
    return row, bool(ok), details


def tail(text, lines=40):
    parts = (text or "").rstrip().splitlines()
    if len(parts) > lines:
        parts = ["..."] + parts[-lines:]
    return "\n".join("    " + p for p in parts)


def print_table(rows):
    columns = [
        ("task", "task"),
        ("lang", "lang"),
        ("difficulty", "difficulty"),
        ("visible_before", "visible (buggy)"),
        ("hidden_before", "hidden (buggy)"),
        ("apply_patch", "solution.patch"),
        ("hidden_after", "hidden (fixed)"),
        ("visible_after", "visible (fixed)"),
        ("result", "result"),
    ]
    widths = [max(len(title), max(len(str(r.get(key, ""))) for r in rows)) for key, title in columns]
    line = "  ".join(title.ljust(w) for (key, title), w in zip(columns, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(r.get(key, "")).ljust(w) for (key, _), w in zip(columns, widths)))


def preflight(python, metas):
    """Fail early with a clear message if a required runner is missing."""
    errors = []
    langs = set(m.get("language") for m in metas)
    if "python" in langs:
        code, out = run([python, "-m", "pytest", "--version"], BENCH_DIR)
        if code != 0:
            errors.append("pytest is not importable by %s (install it, or pass --python "
                          "pointing at an interpreter that has it):\n%s" % (python, tail(out, 5)))
    if "javascript" in langs:
        code, out = run(["node", "--version"], BENCH_DIR)
        if code != 0:
            errors.append("node is not on PATH:\n%s" % tail(out, 5))
    code, _ = run(["git", "--version"], BENCH_DIR)
    if code != 0:
        errors.append("git is not on PATH")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--task", action="append", help="only validate this task id (repeatable)")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter used for commands starting with 'python' (default: %(default)s)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary working copies")
    parser.add_argument("-v", "--verbose", action="store_true", help="show test output for passing steps too")
    args = parser.parse_args(argv)

    if not os.path.isdir(TASKS_DIR):
        print("no tasks directory at %s" % TASKS_DIR)
        return 2
    task_dirs = sorted(
        os.path.join(TASKS_DIR, d) for d in os.listdir(TASKS_DIR)
        if os.path.isdir(os.path.join(TASKS_DIR, d)) and not d.startswith(".")
    )
    if args.task:
        wanted = set(args.task)
        unknown = wanted - set(os.path.basename(d) for d in task_dirs)
        if unknown:
            print("unknown task(s): %s" % ", ".join(sorted(unknown)))
            return 2
        task_dirs = [d for d in task_dirs if os.path.basename(d) in wanted]
    if not task_dirs:
        print("no tasks found")
        return 2

    metas = [check_structure(d)[0] for d in task_dirs]
    errors = preflight(args.python, metas)
    if errors:
        for e in errors:
            print("preflight: " + e)
        return 2

    rows, all_details, failed = [], [], 0
    for task_dir in task_dirs:
        row, ok, details = validate_task(task_dir, args.python, args.keep, args.verbose)
        row["result"] = "OK" if ok else "FAILED"
        failed += 0 if ok else 1
        rows.append(row)
        all_details.extend(details)

    print_table(rows)
    print()
    print("%d/%d tasks valid" % (len(rows) - failed, len(rows)))
    if all_details:
        print()
        for d in all_details:
            print(d)
            print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
