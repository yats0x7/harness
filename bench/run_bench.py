#!/usr/bin/env python3
"""Run the trojan harness against the benchmark tasks and score the results.

For each task under bench/tasks/<id>/ this script:

  1. copies repo/ into a fresh temporary directory and commits it to a new git repo
  2. runs `python -m trojan --headless` on that copy with the task's issue.md
     (output goes to bench/results/<timestamp>/<task>.log)
  3. copies the hidden test into the repo root and runs hidden_test_cmd -> solved if exit 0
  4. runs visible_test_cmd to catch regressions
  5. reads the run's summary.json for status, steps, tokens and cache hit rate

Results are printed as a table and written to bench/results/<timestamp>/results.json
and results.md. The model is chosen by the environment (AI_API_KEY, AI_PROVIDER,
AI_MODEL, ...), which is passed to the harness unchanged.

The harness and any command in meta.json starting with "python" or "python3" run
with the interpreter running this script, so launch it from the project venv:

    .venv/bin/python bench/run_bench.py [--task ID ...] [--max-steps N] [--attempts N]
                                        [--no-review] [--timeout SECONDS] [--keep]

The exit code is 0 whatever the solve rate; it is 2 only for setup errors
(unknown task, missing pytest/node/git).
"""
import argparse
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BENCH_DIR)
TASKS_DIR = os.path.join(BENCH_DIR, "tasks")
RESULTS_DIR = os.path.join(BENCH_DIR, "results")
RUNS_DIR = os.path.join(ROOT_DIR, "runs")
TEST_TIMEOUT = 300
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", "node_modules", ".git")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

GIT = [
    "git",
    "-c", "user.name=bench",
    "-c", "user.email=bench@localhost",
    "-c", "commit.gpgsign=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "init.defaultBranch=main",
]


# ── helpers ──────────────────────────────────────────────────────────────

def resolve_command(cmd, python):
    argv = shlex.split(cmd)
    if argv and argv[0] in ("python", "python3"):
        argv[0] = python
    return argv


def test_env():
    env = dict(os.environ)
    secret_suffixes = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_KEY", "_CREDENTIALS")
    secret_names = {"KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIALS", "AI_API_KEY", "GITHUB_TOKEN",
                    "GH_TOKEN", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "KUBECONFIG", "NETRC",
                    "GIT_ASKPASS", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST"}
    for name in list(env):
        if name in secret_names or name.endswith(secret_suffixes):
            env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPATH", None)
    env["HOME"] = os.path.join(os.getcwd(), ".bench-home")
    env.pop("SSH_AUTH_SOCK", None)
    env.pop("GIT_SSH_COMMAND", None)
    return env


def harness_env(work):
    env = dict(os.environ)
    secret_suffixes = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_KEY", "_CREDENTIALS")
    secret_names = {"KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIALS", "GITHUB_TOKEN", "GH_TOKEN",
                    "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "KUBECONFIG", "NETRC", "GIT_ASKPASS",
                    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST"}
    keep = {"AI_API_KEY", "AI_BASE_URL", "AI_PROVIDER", "AI_MODEL", "AI_THINKING", "AI_FALLBACK_MODELS"}
    for name in list(env):
        if name not in keep and (name in secret_names or name.endswith(secret_suffixes)):
            env.pop(name, None)
    env["HOME"] = os.path.join(work, ".bench-home")
    env.pop("PYTHONPATH", None)
    env.pop("SSH_AUTH_SOCK", None)
    env.pop("GIT_SSH_COMMAND", None)
    return env


def run(argv, cwd, timeout=TEST_TIMEOUT):
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=test_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired as exc:
        out = exc.output or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out + "\n[timed out after %ss]" % timeout
    return proc.returncode, proc.stdout


def hidden_test_file(task_dir):
    for name in ("hidden_test.py", "hidden_test.mjs", "hidden_test.js"):
        if os.path.isfile(os.path.join(task_dir, name)):
            return name
    return None


def load_meta(task_dir):
    with open(os.path.join(task_dir, "meta.json")) as fh:
        return json.load(fh)


def fmt_time(seconds):
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % divmod(seconds, 60)
    h, rest = divmod(seconds, 3600)
    return "%dh%02dm" % (h, rest // 60)


def fmt_int(n):
    return "-" if n is None else "{:,}".format(int(n))


def kill_group(proc):
    """Stop the harness and everything it started (tests, shells)."""
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def test_label(code, argv):
    if code == 0:
        return "pass"
    if code == 124:
        return "timeout"
    if code == 127:
        return "error (not found)"
    if "pytest" in argv and code != 1:
        # 2 interrupted/collection error, 4 usage error, 5 no tests collected
        return "error (exit %d)" % code
    return "FAIL"


# ── finding the run's summary.json ───────────────────────────────────────

def report_path_from_log(log_text):
    """Find `report: <path>` in the harness output. Rich may fold a long path
    over several lines when stdout is not a terminal, so join continuations."""
    lines = [ANSI.sub("", l).rstrip("\n") for l in log_text.splitlines()]
    for i in range(len(lines) - 1, -1, -1):
        m = re.match(r"^\s*report:\s*(.*)$", lines[i])
        if not m:
            continue
        path = m.group(1).strip()
        j = i + 1
        while not path.endswith(".md") and j < len(lines) and j <= i + 5:
            path += lines[j].strip()
            j += 1
        return path
    return None


def find_summary(log_text, run_name, started):
    path = report_path_from_log(log_text)
    if path:
        candidate = os.path.join(os.path.dirname(path), "summary.json")
        if os.path.isfile(candidate):
            return candidate
    # Fallback: newest run directory for this repo created after we started.
    pattern = os.path.join(RUNS_DIR, "*-%s" % run_name, "summary.json")
    fresh = [p for p in glob.glob(pattern) if os.path.getmtime(p) >= started - 5]
    if fresh:
        return max(fresh, key=os.path.getmtime)
    return None


def find_run_dir(run_name, started):
    """The harness's run directory for this task, even if it never wrote summary.json."""
    pattern = os.path.join(RUNS_DIR, "*-%s" % run_name, "trajectory.jsonl")
    fresh = [p for p in glob.glob(pattern) if os.path.getmtime(p) >= started - 5]
    return os.path.dirname(max(fresh, key=os.path.getmtime)) if fresh else None


def partial_from_trajectory(run_dir):
    """Steps and token usage from trajectory.jsonl, for runs killed before they
    wrote summary.json. Usage events carry running totals."""
    info = {"steps": 0}
    pending = False
    try:
        with open(os.path.join(run_dir, "trajectory.jsonl"), errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                kind = e.get("kind")
                if kind == "start":
                    info["provider"] = e.get("provider")
                    info["model"] = e.get("model")
                elif kind == "step":
                    pending = True
                elif kind == "assistant" and pending:
                    info["steps"] += 1  # count steps the model answered
                    pending = False
                elif kind == "usage":
                    info["prompt_tokens"] = e.get("prompt")
                    info["completion_tokens"] = e.get("completion")
                    info["cached_prompt_tokens"] = e.get("cached")
                    info["requests"] = e.get("requests")
    except OSError:
        return None
    if info.get("prompt_tokens"):
        info["cache_hit_percent"] = round(100.0 * (info.get("cached_prompt_tokens") or 0) / info["prompt_tokens"], 1)
    return info


def read_summary(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ── running one task ─────────────────────────────────────────────────────

def harness_argv(args, repo, issue_path):
    argv = [sys.executable, "-m", "trojan", "--headless", "--repo", repo, "--issue", "@" + issue_path]
    if args.max_steps:
        argv += ["--max-steps", str(args.max_steps)]
    if args.attempts:
        argv += ["--attempts", str(args.attempts)]
    if args.no_review:
        argv.append("--no-review")
    return argv


def run_task(task_dir, args, out_dir, index, total):
    task_id = os.path.basename(task_dir)
    meta = load_meta(task_dir)
    row = {
        "task": task_id,
        "difficulty": meta.get("difficulty", "?"),
        "language": meta.get("language", "?"),
        "harness_status": "-",
        "harness_exit": None,
        "timed_out": False,
        "hidden_test": "-",
        "visible_tests": "-",
        "solved": False,
        "steps": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cached_prompt_tokens": None,
        "cache_hit_percent": None,
        "requests": None,
        "wall_seconds": None,
        "provider": None,
        "model": None,
        "summary_path": None,
        "run_dir": None,
        "log": os.path.join(out_dir, task_id + ".log"),
        "workdir": None,
        "error": None,
    }
    prefix = "[%d/%d] %s" % (index, total, task_id)
    print("%s: running (log: %s)" % (prefix, os.path.relpath(row["log"], ROOT_DIR)), flush=True)

    tmp_root = tempfile.mkdtemp(prefix="bench-%s-" % task_id)
    # The leaf is named after the task so the harness names its run dir after it too.
    work = os.path.realpath(os.path.join(tmp_root, task_id))
    row["workdir"] = work
    try:
        shutil.copytree(os.path.join(task_dir, "repo"), work, ignore=IGNORE)
        for argv in (GIT + ["init", "-q"], GIT + ["add", "-A"], GIT + ["commit", "-q", "-m", "task base"]):
            code, out = run(argv, work)
            if code != 0:
                row["error"] = "git setup failed: %s" % out.strip()[-500:]
                row["harness_status"] = "setup-error"
                print("%s: %s" % (prefix, row["error"]), flush=True)
                return row

        issue_path = os.path.join(task_dir, "issue.md")
        argv = harness_argv(args, work, issue_path)
        env = harness_env(work)
        env["PYTHONUNBUFFERED"] = "1"  # so the log can be followed live
        started = time.time()
        with open(row["log"], "w") as log:
            log.write("$ %s\n\n" % " ".join(shlex.quote(a) for a in argv))
            log.flush()
            proc = subprocess.Popen(
                argv, cwd=ROOT_DIR, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                row["harness_exit"] = proc.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                row["timed_out"] = True
                kill_group(proc)
                row["harness_exit"] = proc.returncode
                log.write("\n[bench] killed after %ss timeout\n" % args.timeout)
            except KeyboardInterrupt:
                kill_group(proc)
                raise
        row["wall_seconds"] = round(time.time() - started, 1)

        with open(row["log"], errors="replace") as fh:
            log_text = fh.read()
        summary_path = find_summary(log_text, task_id, started)
        summary = read_summary(summary_path) if summary_path else None
        if summary:
            row["summary_path"] = summary_path
            row["run_dir"] = os.path.dirname(summary_path)
            row["harness_status"] = summary.get("status") or "-"
            attempts = summary.get("attempts") or []
            row["steps"] = sum(int(a.get("steps") or 0) for a in attempts)
            for key in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens",
                        "cache_hit_percent", "requests", "provider", "model"):
                row[key] = summary.get(key)
            if summary.get("error"):
                row["error"] = str(summary["error"])[:500]
        elif not summary:
            run_dir = find_run_dir(task_id, started)
            partial = partial_from_trajectory(run_dir) if run_dir else None
            if partial:
                row["run_dir"] = run_dir
                for key, value in partial.items():
                    row[key] = value
        if row["timed_out"]:
            row["harness_status"] = "timeout"
        elif not summary:
            row["harness_status"] = "no-summary (exit %s)" % row["harness_exit"]

        # Score whatever the agent left in the working tree.
        hidden_name = hidden_test_file(task_dir)
        shutil.copy2(os.path.join(task_dir, hidden_name), os.path.join(work, hidden_name))
        hidden = resolve_command(meta["hidden_test_cmd"], sys.executable)
        code, out = run(hidden, work)
        row["hidden_test"] = test_label(code, hidden)
        row["solved"] = code == 0
        with open(row["log"], "a") as log:
            log.write("\n[bench] hidden test: $ %s -> exit %d\n%s\n" % (" ".join(hidden), code, out))

        visible = resolve_command(meta["visible_test_cmd"], sys.executable)
        code, out = run(visible, work)
        row["visible_tests"] = test_label(code, visible)
        with open(row["log"], "a") as log:
            log.write("\n[bench] visible tests: $ %s -> exit %d\n%s\n" % (" ".join(visible), code, out))
    finally:
        if not args.keep:
            shutil.rmtree(tmp_root, ignore_errors=True)
            row["workdir"] = None

    print("%s: harness %s, hidden %s, visible %s, %s steps, %s in / %s out tokens, %s" % (
        prefix, row["harness_status"], row["hidden_test"], row["visible_tests"],
        "-" if row["steps"] is None else row["steps"],
        fmt_int(row["prompt_tokens"]), fmt_int(row["completion_tokens"]),
        fmt_time(row["wall_seconds"])), flush=True)
    return row


# ── reporting ────────────────────────────────────────────────────────────

COLUMNS = [
    ("task", "task"),
    ("difficulty", "difficulty"),
    ("harness_status", "harness status"),
    ("hidden_test", "hidden test"),
    ("visible_tests", "visible tests"),
    ("steps", "steps"),
    ("tokens", "tokens in/out"),
    ("cache", "cache %"),
    ("time", "time"),
]


def display_row(r):
    tokens = "-"
    if r["prompt_tokens"] is not None or r["completion_tokens"] is not None:
        tokens = "%s / %s" % (fmt_int(r["prompt_tokens"] or 0), fmt_int(r["completion_tokens"] or 0))
    cache = "-" if r["cache_hit_percent"] is None else "%.0f%%" % float(r["cache_hit_percent"])
    return {
        "task": r["task"],
        "difficulty": r["difficulty"],
        "harness_status": r["harness_status"],
        "hidden_test": r["hidden_test"],
        "visible_tests": r["visible_tests"],
        "steps": "-" if r["steps"] is None else str(r["steps"]),
        "tokens": tokens,
        "cache": cache,
        "time": fmt_time(r["wall_seconds"]),
    }


def totals(rows):
    return {
        "tasks": len(rows),
        "solved": sum(1 for r in rows if r["solved"]),
        "visible_regressions": sum(1 for r in rows if r["visible_tests"] not in ("pass", "-")),
        "prompt_tokens": sum(int(r["prompt_tokens"] or 0) for r in rows),
        "completion_tokens": sum(int(r["completion_tokens"] or 0) for r in rows),
        "requests": sum(int(r["requests"] or 0) for r in rows),
        "wall_seconds": round(sum(float(r["wall_seconds"] or 0) for r in rows), 1),
    }


def totals_line(t):
    return "solved %d/%d (hidden test passes), tokens %s in / %s out, time %s" % (
        t["solved"], t["tasks"], fmt_int(t["prompt_tokens"]), fmt_int(t["completion_tokens"]),
        fmt_time(t["wall_seconds"]))


def text_table(rows):
    shown = [display_row(r) for r in rows]
    widths = [max(len(title), max(len(s[key]) for s in shown)) for key, title in COLUMNS]
    lines = ["  ".join(title.ljust(w) for (_, title), w in zip(COLUMNS, widths)),
             "  ".join("-" * w for w in widths)]
    for s in shown:
        lines.append("  ".join(s[key].ljust(w) for (key, _), w in zip(COLUMNS, widths)))
    return "\n".join(lines)


def markdown_table(rows):
    shown = [display_row(r) for r in rows]
    lines = ["| " + " | ".join(title for _, title in COLUMNS) + " |",
             "|" + "|".join("---" for _ in COLUMNS) + "|"]
    for s in shown:
        lines.append("| " + " | ".join(s[key].replace("|", "\\|") for key, _ in COLUMNS) + " |")
    return "\n".join(lines)


def write_results(out_dir, rows, args, stamp):
    t = totals(rows)
    models = sorted(set("%s/%s" % (r["provider"], r["model"]) for r in rows if r["model"]))
    settings = {
        "max_steps": args.max_steps,
        "attempts": args.attempts,
        "review": not args.no_review,
        "timeout": args.timeout,
    }
    with open(os.path.join(out_dir, "results.json"), "w") as fh:
        json.dump({"timestamp": stamp, "models": models, "settings": settings,
                   "totals": t, "tasks": rows}, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(out_dir, "results.md"), "w") as fh:
        fh.write("# Benchmark results %s\n\n" % stamp)
        fh.write("Model: %s  \n" % (", ".join(models) or "unknown"))
        fh.write("Settings: max steps %s, attempts %s, review %s, timeout %ss\n\n" % (
            args.max_steps or "default", args.attempts or "default",
            "off" if args.no_review else "on", args.timeout))
        fh.write(markdown_table(rows) + "\n\n")
        fh.write(totals_line(t) + "\n")
    return t


# ── setup ────────────────────────────────────────────────────────────────

def list_tasks(wanted):
    if not os.path.isdir(TASKS_DIR):
        return None, "no tasks directory at %s" % TASKS_DIR
    task_dirs = sorted(
        os.path.join(TASKS_DIR, d) for d in os.listdir(TASKS_DIR)
        if os.path.isdir(os.path.join(TASKS_DIR, d)) and not d.startswith(".")
    )
    if wanted:
        known = set(os.path.basename(d) for d in task_dirs)
        unknown = [w for w in wanted if w not in known]
        if unknown:
            return None, "unknown task(s): %s (known: %s)" % (", ".join(unknown), ", ".join(sorted(known)))
        order = {w: i for i, w in enumerate(wanted)}
        task_dirs = sorted((d for d in task_dirs if os.path.basename(d) in order),
                           key=lambda d: order[os.path.basename(d)])
    if not task_dirs:
        return None, "no tasks found"
    problems = []
    for d in task_dirs:
        name = os.path.basename(d)
        if not os.path.isdir(os.path.join(d, "repo")):
            problems.append("%s: missing repo/" % name)
        if not os.path.isfile(os.path.join(d, "issue.md")):
            problems.append("%s: missing issue.md" % name)
        if hidden_test_file(d) is None:
            problems.append("%s: missing hidden_test.py/.mjs" % name)
        try:
            meta = load_meta(d)
            for key in ("hidden_test_cmd", "visible_test_cmd"):
                if key not in meta:
                    problems.append("%s: meta.json missing %s" % (name, key))
        except (OSError, ValueError) as exc:
            problems.append("%s: bad meta.json: %s" % (name, exc))
    if problems:
        return None, "\n".join(problems)
    return task_dirs, None


def preflight(task_dirs):
    errors = []
    langs = set(load_meta(d).get("language") for d in task_dirs)
    code, out = run([sys.executable, "-m", "trojan", "--version"], ROOT_DIR, timeout=60)
    if code != 0:
        errors.append("the harness does not start with %s (run `make setup`):\n%s" % (sys.executable, out.strip()[-500:]))
    if "python" in langs:
        code, out = run([sys.executable, "-m", "pytest", "--version"], ROOT_DIR, timeout=60)
        if code != 0:
            errors.append("pytest is not importable by %s:\n%s" % (sys.executable, out.strip()[-500:]))
    if "javascript" in langs:
        code, _ = run(["node", "--version"], ROOT_DIR, timeout=60)
        if code != 0:
            errors.append("node is not on PATH (needed for JavaScript tasks)")
    code, _ = run(["git", "--version"], ROOT_DIR, timeout=60)
    if code != 0:
        errors.append("git is not on PATH")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--task", action="append", help="run only this task id (repeatable; default: all)")
    parser.add_argument("--max-steps", type=int, help="passed to the harness as --max-steps")
    parser.add_argument("--attempts", type=int, help="passed to the harness as --attempts")
    parser.add_argument("--no-review", action="store_true", help="passed to the harness as --no-review")
    parser.add_argument("--timeout", type=int, default=1800, help="per-task limit in seconds (default: %(default)s)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary working copies")
    args = parser.parse_args(argv)

    task_dirs, error = list_tasks(args.task)
    if error:
        print(error)
        return 2
    errors = preflight(task_dirs)
    if errors:
        for e in errors:
            print("preflight: " + e)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(RESULTS_DIR, stamp)
    os.makedirs(out_dir, exist_ok=True)
    print("bench: %d task(s), results in %s" % (len(task_dirs), os.path.relpath(out_dir, ROOT_DIR)), flush=True)

    rows = []
    interrupted = False
    try:
        for i, task_dir in enumerate(task_dirs, 1):
            rows.append(run_task(task_dir, args, out_dir, i, len(task_dirs)))
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted; reporting the tasks that finished", flush=True)

    if not rows:
        return 130 if interrupted else 0
    t = write_results(out_dir, rows, args, stamp)
    print()
    print(text_table(rows))
    print()
    print(totals_line(t))
    if args.keep:
        for r in rows:
            if r["workdir"]:
                print("kept %s: %s" % (r["task"], r["workdir"]))
    print("wrote %s and results.md" % os.path.relpath(os.path.join(out_dir, "results.json"), ROOT_DIR))
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
