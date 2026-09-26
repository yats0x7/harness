"""The repository the agent works on, and everything that touches its disk.

The harness never asks the model for a patch. It records the state of the repo
at the start (a commit object that includes any uncommitted local changes) and
reads the final patch back from git, which is both exact and cheap.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache",
             ".tox", "dist", "build", "target", ".next", ".idea", ".vscode", "vendor", ".gradle", "coverage",
             ".ruff_cache", "site-packages", ".eggs"}


@dataclass
class CommandResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False
    duration: float = 0.0


def run(cmd, cwd: Path, timeout: float = 120, env: Optional[Dict[str, str]] = None,
        shell: bool = False) -> CommandResult:
    """Run a command with no stdin, merged output, and a hard timeout that kills the whole group."""
    start = time.time()
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), shell=shell, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        executable="/bin/bash" if shell else None,
        start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, _ = proc.communicate()
        timed_out = True
    text = out.decode("utf-8", errors="replace") if out else ""
    shown = cmd if isinstance(cmd, str) else " ".join(cmd)
    return CommandResult(shown, proc.returncode if not timed_out else -9, text, timed_out, time.time() - start)


def git(root: Path, *args: str, timeout: float = 60) -> CommandResult:
    return run(["git", "-c", "core.quotepath=off", *args], cwd=root, timeout=timeout)


def _python_with_pytest(root: Path) -> str:
    """Prefer the repo's own venv, then the system Python, then ours."""
    candidates = [root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"]
    candidates += [Path(p) for p in (shutil.which("python3"), shutil.which("python")) if p]
    candidates.append(Path(sys.executable))
    for cand in candidates:
        if cand.exists():
            probe = run([str(cand), "-c", "import pytest"], cwd=root, timeout=30)
            if probe.exit_code == 0:
                return str(cand)
    return str(candidates[0] if candidates[0].exists() else sys.executable)


def detect_test_command(root: Path) -> Tuple[Optional[str], str]:
    """Best guess at the project's test command. Returns (command, language)."""
    has = lambda name: (root / name).exists()
    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except (ValueError, OSError):
            scripts = {}
        test = scripts.get("test", "")
        if test and "no test specified" not in test:
            runner = "pnpm" if has("pnpm-lock.yaml") else "yarn" if has("yarn.lock") else "npm"
            return (f"{runner} test" if runner != "npm" else "npm test --silent"), "javascript"
        return "node --test", "javascript"
    if has("go.mod"):
        return "go test ./...", "go"
    if has("Cargo.toml"):
        return "cargo test", "rust"
    if has("pom.xml"):
        return "mvn -q test", "java"
    if has("build.gradle") or has("build.gradle.kts"):
        return ("./gradlew test" if has("gradlew") else "gradle test"), "java"
    py_markers = ["pytest.ini", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "conftest.py"]
    if any(has(m) for m in py_markers) or list(root.glob("test*/**/*.py")) or list(root.glob("test_*.py")):
        return "python -m pytest -q -p no:cacheprovider", "python"
    if has("Gemfile"):
        return "bundle exec rake test", "ruby"
    if has("Makefile") and re.search(r"^test:", (root / "Makefile").read_text(errors="ignore"), re.M):
        return "make test", "make"
    js_tests = [p for p in root.rglob("*.test.*js") if not SKIP_DIRS & set(p.parts)][:1]
    if js_tests:
        return "node --test", "javascript"
    return None, "unknown"


@dataclass
class Workspace:
    root: Path
    run_dir: Path
    base_ref: str = "HEAD"
    initial_untracked: Set[str] = field(default_factory=set)
    test_command: Optional[str] = None
    language: str = "unknown"
    notes: List[str] = field(default_factory=list)

    @property
    def scratch(self) -> Path:
        return self.run_dir / "scratch"

    @property
    def outputs(self) -> Path:
        return self.run_dir / "outputs"

    @property
    def bin_dir(self) -> Path:
        return self.run_dir / "bin"

    # ── setup ────────────────────────────────────────────────────────────
    @classmethod
    def prepare(cls, repo: str, runs_root: Path, workspace_root: Path) -> "Workspace":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        notes: List[str] = []
        if re.match(r"^(https?://|git@|ssh://)", repo) or repo.endswith(".git"):
            name = re.sub(r"\.git$", "", repo.rstrip("/").split("/")[-1]) or "repo"
            dest = workspace_root / f"{name}-{stamp}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Partial clone: full history, file contents fetched on demand. Much faster on big repos.
            res = run(["git", "clone", "--quiet", "--filter=blob:none", repo, str(dest)], cwd=workspace_root, timeout=900)
            if res.exit_code != 0:
                shutil.rmtree(dest, ignore_errors=True)
                res = run(["git", "clone", "--quiet", repo, str(dest)], cwd=workspace_root, timeout=900)
            if res.exit_code != 0:
                raise RuntimeError(f"git clone failed:\n{res.output[-2000:]}")
            root = dest
            notes.append(f"cloned {repo} into {dest}")
        else:
            root = Path(repo).expanduser().resolve()
            if not root.is_dir():
                raise RuntimeError(f"repository path does not exist: {root}")

        if git(root, "rev-parse", "--is-inside-work-tree").exit_code != 0:
            git(root, "init", "-q")
            git(root, "add", "-A")
            run(["git", "-c", "user.name=trojan", "-c", "user.email=trojan@localhost", "commit", "-q",
                 "-m", "trojan baseline", "--allow-empty"], cwd=root)
            notes.append("the folder was not a git repository; initialised one to track changes")
        else:
            # Anchor at the repository top level so paths line up with git's.
            top = git(root, "rev-parse", "--show-toplevel").output.strip()
            if top:
                root = Path(top)
        if git(root, "rev-parse", "--verify", "HEAD").exit_code != 0:
            run(["git", "-c", "user.name=trojan", "-c", "user.email=trojan@localhost", "commit", "-q",
                 "--allow-empty", "-m", "trojan baseline"], cwd=root)

        # `git stash create` snapshots tracked local edits without touching the tree.
        stash = git(root, "stash", "create").output.strip()
        base_ref = stash or git(root, "rev-parse", "HEAD").output.strip()
        if stash:
            notes.append("the repository had uncommitted changes; they are part of the baseline")
        untracked = set(git(root, "ls-files", "--others", "--exclude-standard", "-z").output.split("\0")) - {""}

        run_dir = runs_root / f"{stamp}-{root.name}"
        ws = cls(root=root, run_dir=run_dir, base_ref=base_ref, initial_untracked=untracked, notes=notes)
        for d in (ws.run_dir, ws.scratch, ws.outputs, ws.bin_dir):
            d.mkdir(parents=True, exist_ok=True)
        ws.test_command, ws.language = detect_test_command(root)
        if ws.language == "python":
            py = _python_with_pytest(root)
            # A wrapper, not a symlink: a venv is only recognised from its real path.
            for name in ("python", "python3"):
                shim = ws.bin_dir / name
                shim.write_text(f'#!/bin/sh\nexec "{py}" "$@"\n')
                shim.chmod(0o755)
        return ws

    # ── environment for agent commands ───────────────────────────────────
    def env(self, root: Optional[Path] = None) -> Dict[str, str]:
        root = root or self.root
        env = dict(os.environ)
        env.pop("AI_API_KEY", None)  # the model's shell never sees the key
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["PYTHONPATH"] = f"{root}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
        env.update({"SCRATCH": str(self.scratch), "REPO": str(root), "PAGER": "cat", "GIT_PAGER": "cat",
                    "CI": "1", "TERM": "dumb", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1",
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1", "GIT_TERMINAL_PROMPT": "0",
                    "DEBIAN_FRONTEND": "noninteractive"})
        return env

    def shell(self, command: str, timeout: float, cwd: Optional[Path] = None) -> CommandResult:
        """Run a command for the agent. With `cwd` set to another checkout, it runs against that one."""
        return run(command, cwd=cwd or self.root, timeout=timeout, env=self.env(cwd), shell=True)

    # ── paths ────────────────────────────────────────────────────────────
    def resolve(self, path: str) -> Path:
        """Map a model-supplied path to a real one inside the repo or scratch dir."""
        path = (path or "").strip()
        path = path.replace("$SCRATCH", str(self.scratch)).replace("$REPO", str(self.root))
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        for allowed in (self.root.resolve(), self.scratch.resolve()):
            if p == allowed or allowed in p.parents:
                return p
        raise ValueError(f"path is outside the repository: {path}")

    def rel(self, p: Path) -> str:
        try:
            return str(p.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(p)

    # ── diffs ────────────────────────────────────────────────────────────
    def new_files(self) -> List[str]:
        now = set(git(self.root, "ls-files", "--others", "--exclude-standard", "-z").output.split("\0")) - {""}
        return sorted(f for f in now - self.initial_untracked
                      if not any(part in SKIP_DIRS for part in Path(f).parts) and not f.endswith((".pyc", ".pyo")))

    def diff(self) -> str:
        parts = [git(self.root, "diff", "--no-color", "--no-ext-diff", self.base_ref).output]
        for f in self.new_files():
            res = run(["git", "diff", "--no-color", "--no-index", "--", "/dev/null", f], cwd=self.root)
            parts.append(res.output)
        return "".join(p for p in parts if p)

    def changed_files(self) -> List[str]:
        names = git(self.root, "diff", "--name-only", self.base_ref).output.split()
        return sorted(set(names) | set(self.new_files()))

    def reset_to_base(self) -> None:
        """Undo every change the agent made (used between attempts)."""
        git(self.root, "checkout", "-q", self.base_ref, "--", ".")
        for f in self.new_files():
            try:
                (self.root / f).unlink()
            except OSError:
                pass

    def apply_patch(self, patch: str) -> bool:
        if not patch.strip():
            return True
        path = self.run_dir / "apply.patch"
        path.write_text(patch)
        return git(self.root, "apply", "--whitespace=nowarn", str(path)).exit_code == 0

    def base_worktree(self) -> Optional[Path]:
        """A throwaway checkout of the starting state, for before/after checks."""
        dest = Path(tempfile.mkdtemp(prefix="trojan-base-"))
        dest.rmdir()
        res = git(self.root, "worktree", "add", "--detach", "-f", str(dest), self.base_ref, timeout=300)
        return dest if res.exit_code == 0 else None

    def drop_worktree(self, path: Path) -> None:
        git(self.root, "worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)

    # ── repo overview for the first prompt ───────────────────────────────
    def tracked_files(self) -> List[str]:
        out = git(self.root, "ls-files", "-z").output.split("\0")
        return [f for f in out if f]

    def overview(self, max_lines: int = 60) -> str:
        files = self.tracked_files()
        exts: Dict[str, int] = {}
        for f in files:
            ext = Path(f).suffix or Path(f).name
            exts[ext] = exts.get(ext, 0) + 1
        top_exts = ", ".join(f"{e} ({n})" for e, n in sorted(exts.items(), key=lambda kv: -kv[1])[:8])
        tree: List[str] = []
        seen: Set[str] = set()
        visible = [f for f in files if not any(part.startswith(".") for part in Path(f).parts)]
        for f in sorted(visible):
            parts = Path(f).parts
            for depth in range(min(len(parts), 3)):
                key = "/".join(parts[: depth + 1])
                if key in seen:
                    continue
                seen.add(key)
                is_dir = depth < len(parts) - 1
                tree.append("  " * depth + parts[depth] + ("/" if is_dir else ""))
        if len(tree) > max_lines:
            tree = tree[:max_lines] + [f"... ({len(files)} files in total)"]
        return f"{len(files)} tracked files; most common: {top_exts}\n" + "\n".join(tree)
