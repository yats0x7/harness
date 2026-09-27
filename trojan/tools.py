"""The agent's tools.

A small, sharp set: shell, read, search, find, list, edit, write, undo, diff,
tests, plan and finish. The design follows what measurably helps a fixed model
(SWE-agent's interface ablations, Anthropic's str_replace editor): a windowed
file viewer, capped search results, exact-match edits with precise errors, and
a syntax check that rolls back any edit which breaks the file.
"""
from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .workspace import SKIP_DIRS, CommandResult, Workspace, run

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

READ_WINDOW = 250
READ_MAX = 600
MIN_WINDOW = 100
SEARCH_LIMIT = 50

_BLOCKED = [
    (re.compile(r"\bgit\s+push\b"), "pushing is not allowed"),
    (re.compile(r"\bsudo\b"), "sudo is not allowed"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME)(\s|/?$)"), "refusing to delete a root or home directory"),
    (re.compile(r"\b(mkfs|shutdown|reboot|halt)\b"), "system commands are not allowed"),
    (re.compile(r":\(\)\s*\{"), "fork bombs are not allowed"),
    (re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+(--\s+)?\.(\s|$)|stash\b)"),
     "this would discard work; use undo_edit to revert a file"),
    (re.compile(r"^\s*(vi|vim|nvim|nano|emacs|less|more|top|htop|man)\b"), "interactive programs cannot run here"),
    (re.compile(r"\bgit\s+commit\b"), "do not commit; the harness collects your changes from the working tree"),
]
_VERIFY_HINT = re.compile(
    r"\b(pytest|python3?|node|npm|npx|yarn|pnpm|jest|vitest|mocha|go\s+test|cargo\s+test|mvn|gradle|gradlew|"
    r"make\s+test|tox|unittest|ruby|rspec|bundle|php|phpunit|dotnet\s+test|bash\s+\S+\.sh|sh\s+\S+\.sh)\b")
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|testing)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$|(^|/)conftest\.py$")
_LINE_PREFIX = re.compile(r"^\s*\d+\t")


def is_test_path(rel: str) -> bool:
    return bool(_TEST_PATH.search(rel.replace("\\", "/")))


@dataclass
class ToolState:
    step: int = 0
    seq: int = 0  # increments on every tool call, so order within one message counts
    last_edit_step: int = -1
    last_verify_step: int = -1
    last_verify_ok: bool = False
    edited: Set[str] = field(default_factory=set)
    tests_modified: Set[str] = field(default_factory=set)
    history: Dict[str, List[Optional[str]]] = field(default_factory=dict)
    runs: List[Dict[str, Any]] = field(default_factory=list)
    plan: str = ""
    test_targets: List[str] = field(default_factory=list)
    finish_args: Optional[Dict[str, Any]] = None
    output_seq: int = 0


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    fn: Callable[[Dict[str, Any]], str]

    def schema(self) -> Dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def _obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def check_syntax(path: Path) -> Optional[str]:
    """Return a one-line syntax error for files we can check cheaply, else None."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            compile(path.read_text(encoding="utf-8", errors="replace"), str(path), "exec", dont_inherit=True)
        elif suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
        elif suffix == ".toml":
            tomllib.loads(path.read_text(encoding="utf-8"))
        elif suffix in (".js", ".mjs", ".cjs") and shutil.which("node"):
            res = run(["node", "--check", str(path)], cwd=path.parent, timeout=30)
            if res.exit_code != 0:
                lines = [l for l in res.output.strip().splitlines() if l.strip()]
                return " | ".join(lines[:4])[:400]
    except SyntaxError as exc:
        return f"line {exc.lineno}: {exc.msg}"
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        return str(exc)[:300]
    return None


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


class Toolbox:
    def __init__(self, ws: Workspace, output_chars: int, command_timeout: int, test_timeout: int):
        self.ws = ws
        self.output_chars = output_chars
        self.command_timeout = command_timeout
        self.test_timeout = test_timeout
        self.state = ToolState()
        self._rg = shutil.which("rg") if os.path.isfile(shutil.which("rg") or "") else None
        self.tools: Dict[str, Tool] = {t.name: t for t in self._build()}

    # ── plumbing ─────────────────────────────────────────────────────────
    def schemas(self) -> List[Dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def parameter_schemas(self) -> Dict[str, dict]:
        return {t.name: t.parameters for t in self.tools.values()}

    def call(self, name: str, args: Dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'. Available tools: {', '.join(self.tools)}."
        self.state.seq += 1
        missing = [r for r in tool.parameters.get("required", []) if r not in args]
        if missing:
            return f"Error: {name} is missing required argument(s): {', '.join(missing)}."
        try:
            return tool.fn(args)
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # a tool bug must not kill the run
            return f"Error: {name} failed with {type(exc).__name__}: {exc}"

    def _save_output(self, text: str) -> Path:
        self.state.output_seq += 1
        path = self.ws.outputs / f"{self.state.output_seq:03d}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def truncate(self, text: str, limit: Optional[int] = None) -> str:
        limit = limit or self.output_chars
        if len(text) <= limit:
            return text
        saved = self._save_output(text)
        head = text[: limit // 3]
        tail = text[-(limit - len(head)):]
        dropped = text[len(head): len(text) - len(tail)]
        return (f"{head}\n\n[... {dropped.count(chr(10))} lines ({len(dropped)} chars) omitted. "
                f"Full output saved to {saved}; search or read it if you need the middle.]\n\n{tail}")

    def _record_run(self, res: CommandResult, kind: str) -> None:
        self.state.runs.append({"step": self.state.step, "command": res.command, "exit_code": res.exit_code,
                                "kind": kind, "timed_out": res.timed_out})
        if kind == "test" or _VERIFY_HINT.search(res.command):
            self.state.last_verify_step = self.state.seq
            self.state.last_verify_ok = res.exit_code == 0

    def _remember(self, path: Path) -> None:
        rel = self.ws.rel(path)
        prev = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
        self.state.history.setdefault(rel, []).append(prev)

    def _mark_edit(self, path: Path) -> str:
        rel = self.ws.rel(path)
        in_scratch = self.ws.scratch.resolve() in path.resolve().parents
        if not in_scratch:
            self.state.edited.add(rel)
            self.state.last_edit_step = self.state.seq
        note = ""
        if not in_scratch and is_test_path(rel):
            existed = self.state.history.get(rel, [None])[0] is not None
            if existed:
                self.state.tests_modified.add(rel)
                note = ("\nNote: you changed an existing test file. Only do this if the issue requires it. "
                        "Never weaken or delete assertions to make tests pass; the reviewer will check.")
        return note

    def _snippet(self, path: Path, start: int, end: int, context: int = 3) -> str:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        lo, hi = max(1, start - context), min(len(lines), end + context)
        return "\n".join(f"{n:>5}\t{lines[n - 1]}" for n in range(lo, hi + 1))

    # ── tool implementations ─────────────────────────────────────────────
    def bash(self, a: Dict[str, Any]) -> str:
        command = str(a["command"]).strip()
        if not command:
            return "Error: empty command."
        for pattern, why in _BLOCKED:
            if pattern.search(command):
                return f"Error: blocked ({why})."
        timeout = min(int(a.get("timeout") or self.command_timeout), max(self.test_timeout, self.command_timeout))
        res = self.ws.shell(command, timeout=timeout)
        self._record_run(res, "bash")
        head = f"exit code: {res.exit_code}" + (f" (timed out after {timeout}s)" if res.timed_out else "")
        body = res.output.rstrip() or "(no output)"
        note = ""
        runs_scratch = "$SCRATCH" in command or str(self.ws.scratch) in command
        if runs_scratch and res.exit_code == 0 and not self.state.edited:
            note = ("\n[Harness: this script exited 0 on the unfixed code, so it does not detect the bug yet. "
                    "Make it assert the expected behaviour so it fails now and passes after the fix.]")
        return f"{head}\n{self.truncate(body)}{note}"

    def read_file(self, a: Dict[str, Any]) -> str:
        path = self.ws.resolve(a["path"])
        if path.is_dir():
            return f"Error: {a['path']} is a directory. Use list_dir."
        if not path.exists():
            return f"Error: {a['path']} does not exist. Use find_files to locate it."
        raw = path.read_bytes()
        if b"\0" in raw[:4096]:
            return f"Error: {a['path']} looks like a binary file."
        lines = raw.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        start = max(1, int(a.get("start_line") or 1))
        end = int(a.get("end_line") or start + READ_WINDOW - 1)
        # Models tend to read in tiny slices and burn a turn per slice. Anything
        # under MIN_WINDOW lines is widened (SWE-agent found ~100-line views best).
        if end - start + 1 < MIN_WINDOW:
            start = max(1, start - 10)
            end = start + MIN_WINDOW - 1
        end = min(total, end, start + READ_MAX - 1)
        if total == 0:
            return f"{self.ws.rel(path)} is empty."
        if start > total:
            return f"Error: start_line {start} is past the end ({total} lines)."
        body = "\n".join(f"{n:>5}\t{lines[n - 1][:400]}" for n in range(start, end + 1))
        more = f"\n[{total - end} more lines; call read_file with start_line={end + 1} to continue]" if end < total else ""
        return f"{self.ws.rel(path)} (lines {start}-{end} of {total})\n{body}{more}"

    def _all_files(self) -> List[str]:
        files = self.ws.tracked_files() + self.ws.new_files()
        return [f for f in dict.fromkeys(files) if not any(p in SKIP_DIRS for p in Path(f).parts)]

    def search(self, a: Dict[str, Any]) -> str:
        pattern = str(a["pattern"])
        fixed = bool(a.get("fixed_string", False))
        glob = a.get("glob") or None
        base = self.ws.resolve(a.get("path") or ".")
        ignore_case = pattern == pattern.lower()
        lines: List[str] = []
        if self._rg:
            cmd = [self._rg, "-n", "--no-heading", "--color=never", "--max-columns=300"]
            cmd += ["-F"] if fixed else []
            cmd += ["-i"] if ignore_case else []
            cmd += ["-g", glob] if glob else []
            cmd += ["--", pattern, str(base)]
            res = run(cmd, cwd=self.ws.root, timeout=60)
            root_prefix = str(self.ws.root) + os.sep
            lines = [l.replace(root_prefix, "", 1) for l in res.output.splitlines() if l.strip()]
        else:
            try:
                rx = re.compile(re.escape(pattern) if fixed else pattern, re.I if ignore_case else 0)
            except re.error as exc:
                return f"Error: invalid regex ({exc}). Set fixed_string=true to search literally."
            base_rel = self.ws.rel(base)
            for f in self._all_files():
                if base_rel not in (".", "") and not (f == base_rel or f.startswith(base_rel.rstrip("/") + "/")):
                    continue
                if glob and not (fnmatch.fnmatch(f, glob) or fnmatch.fnmatch(Path(f).name, glob)):
                    continue
                p = self.ws.root / f
                try:
                    if p.stat().st_size > 1_500_000:
                        continue
                    data = p.read_bytes()
                except OSError:
                    continue
                if b"\0" in data[:2048]:
                    continue
                for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        lines.append(f"{f}:{n}:{line.strip()[:300]}")
        if not lines:
            return f"No matches for {pattern!r}."
        shown = lines[:SEARCH_LIMIT]
        out = "\n".join(shown)
        if len(lines) > SEARCH_LIMIT:
            per_file: Dict[str, int] = {}
            for l in lines[SEARCH_LIMIT:]:
                per_file[l.split(":", 1)[0]] = per_file.get(l.split(":", 1)[0], 0) + 1
            top = ", ".join(f"{k} ({v})" for k, v in sorted(per_file.items(), key=lambda kv: -kv[1])[:10])
            out += f"\n[{len(lines) - SEARCH_LIMIT} more matches, in: {top}. Narrow with path or glob.]"
        return out

    def find_files(self, a: Dict[str, Any]) -> str:
        pattern = str(a["pattern"]).strip()
        files = self._all_files()
        if any(c in pattern for c in "*?["):
            hits = [f for f in files if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(Path(f).name, pattern)]
        else:
            hits = [f for f in files if pattern.lower() in f.lower()]
        if not hits:
            return f"No files match {pattern!r}."
        more = f"\n[{len(hits) - 100} more]" if len(hits) > 100 else ""
        return "\n".join(hits[:100]) + more

    def list_dir(self, a: Dict[str, Any]) -> str:
        base = self.ws.resolve(a.get("path") or ".")
        depth = max(1, min(int(a.get("depth") or 2), 4))
        if not base.is_dir():
            return f"Error: {a.get('path')} is not a directory."
        out: List[str] = []
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            level = len(Path(dirpath).parts) - base_depth
            if level >= depth:
                dirnames[:] = []
            indent = "  " * level
            if level > 0:
                out.append(f"{indent[:-2]}{Path(dirpath).name}/")
            for fn in sorted(filenames):
                out.append(f"{indent}{fn}")
            if len(out) > 300:
                out.append("[... truncated; list a subdirectory]")
                break
        return "\n".join(out) or "(empty)"

    def _strip_line_numbers(self, text: str) -> str:
        lines = text.split("\n")
        nonblank = [l for l in lines if l.strip()]
        if nonblank and all(_LINE_PREFIX.match(l) for l in nonblank):
            return "\n".join(_LINE_PREFIX.sub("", l) for l in lines)
        return text

    def edit_file(self, a: Dict[str, Any]) -> str:
        path = self.ws.resolve(a["path"])
        old = self._strip_line_numbers(str(a["old_str"]))
        new = self._strip_line_numbers(str(a.get("new_str", "")))
        replace_all = bool(a.get("replace_all", False))
        if not path.exists():
            return f"Error: {a['path']} does not exist. Use write_file to create a new file."
        if not old:
            return "Error: old_str is empty. To create or overwrite a whole file use write_file."
        if old == new:
            return "Error: old_str and new_str are identical; nothing to change."
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        note = ""
        if count > 1 and not replace_all:
            positions, idx = [], text.find(old)
            while idx != -1:
                positions.append(text[:idx].count("\n") + 1)
                idx = text.find(old, idx + 1)
            return (f"Error: old_str occurs {count} times (starting at lines {', '.join(map(str, positions[:10]))}). "
                    "Include more surrounding lines to make it unique, or set replace_all=true.")
        alternative = None
        if count >= 1:
            idx = text.find(old)
            start_line = text[:idx].count("\n") + 1
            new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            # A multi-line new_str pasted at an indented position often lacks the
            # indentation on its later lines. Keep a re-indented variant ready.
            line_start = text.rfind("\n", 0, idx) + 1
            lead = text[line_start:idx]
            if lead and not lead.strip() and "\n" in new and not replace_all:
                head, *rest = new.split("\n")
                alt_new = "\n".join([head] + [lead + l if l.strip() else l for l in rest])
                alternative = text.replace(old, alt_new, 1)
        else:
            fuzzy = self._fuzzy_replace(text, old, new)
            if fuzzy is None:
                if new.strip() and new.strip() in text:
                    line = text[: text.find(new.strip())].count("\n") + 1
                    return (f"This change is already applied: new_str is already in {self.ws.rel(path)} at line "
                            f"{line}, and old_str is gone. Do not repeat the edit. Move on: run the reproduction "
                            "and the tests.")
                return self._no_match_error(path, text, old)
            new_text, start_line = fuzzy
            note = " (matched after ignoring whitespace differences)"

        broke_before = check_syntax(path)
        self._remember(path)
        path.write_text(new_text, encoding="utf-8")
        err = check_syntax(path)
        if err and not broke_before and alternative is not None:
            path.write_text(alternative, encoding="utf-8")
            err = check_syntax(path)
            if not err:
                note = " (indentation of the new lines adjusted to match)"
                new = alt_new
            else:
                path.write_text(new_text, encoding="utf-8")
                err = check_syntax(path)
        if err and not broke_before:
            prev = self.state.history[self.ws.rel(path)].pop()
            path.write_text(prev or "", encoding="utf-8")
            return (f"Error: edit rejected because it breaks the file's syntax ({err}). "
                    "The file is unchanged. Check indentation and brackets and try again.")
        end_line = start_line + max(new.count("\n"), 0)
        warn = self._mark_edit(path)
        tail = f"\nWarning: the file still has a syntax error: {err}" if err else ""
        return (f"Edited {self.ws.rel(path)}{note}. Lines {start_line}-{end_line} now read:\n"
                f"{self._snippet(path, start_line, end_line)}{warn}{tail}")

    def _fuzzy_replace(self, text: str, old: str, new: str):
        lines = text.split("\n")
        old_lines = old.strip("\n").split("\n")
        new_lines = new.strip("\n").split("\n") if new.strip("\n") else []
        n = len(old_lines)
        if n == 0:
            return None
        for relaxed in (lambda s: s.rstrip(), lambda s: s.strip()):
            hits = [i for i in range(len(lines) - n + 1)
                    if all(relaxed(lines[i + j]) == relaxed(old_lines[j]) for j in range(n))]
            if len(hits) != 1:
                continue
            i = hits[0]
            first_old = next((l for l in old_lines if l.strip()), "")
            first_file = next((lines[i + j] for j in range(n) if lines[i + j].strip()), "")
            have, want = _indent(first_old), _indent(first_file)
            adjusted = []
            for l in new_lines:
                if not l.strip():
                    adjusted.append("")
                elif l.startswith(have):
                    adjusted.append(want + l[len(have):])
                else:
                    adjusted.append(want + l.lstrip())
            result = lines[:i] + adjusted + lines[i + n:]
            return "\n".join(result), i + 1
        return None

    def _no_match_error(self, path: Path, text: str, old: str) -> str:
        lines = text.split("\n")
        old_lines = old.strip("\n").split("\n")
        n = len(old_lines)
        best, best_i = 0.0, 0
        target = "\n".join(l.strip() for l in old_lines)
        for i in range(max(1, len(lines) - n + 1)):
            window = "\n".join(l.strip() for l in lines[i:i + n])
            sm = difflib.SequenceMatcher(None, window, target, autojunk=False)
            if sm.real_quick_ratio() < best or sm.quick_ratio() < best:
                continue
            r = sm.ratio()
            if r > best:
                best, best_i = r, i
            if i > 20000:
                break
        hint = ""
        if best > 0.5:
            region = lines[best_i:best_i + n]
            diffs = []
            for j, (want, have) in enumerate(zip(old_lines, region)):
                if want != have:
                    diffs.append(f"  your line {j + 1}: {want!r}\n  file line {best_i + j + 1}: {have!r}")
            shown = "\n".join(diffs[:4])
            hint = (f"\nThe closest text (similarity {best:.0%}) is at lines {best_i + 1}-{best_i + n}. "
                    f"Lines that differ:\n{shown}\nTo edit that region, use exactly this as old_str:\n"
                    f"<<<\n" + "\n".join(region) + "\n>>>")
        return (f"Error: old_str was not found in {self.ws.rel(path)}. It must match the file exactly, "
                f"including indentation. Re-read the file and copy the text precisely.{hint}")

    def write_file(self, a: Dict[str, Any]) -> str:
        path = self.ws.resolve(a["path"])
        content = str(a.get("content", ""))
        existed = path.exists()
        self._remember(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        warn = self._mark_edit(path)
        err = check_syntax(path)
        tail = f"\nWarning: syntax error in the new content: {err}" if err else ""
        verb = "Overwrote" if existed else "Created"
        return f"{verb} {self.ws.rel(path)} ({content.count(chr(10)) + 1} lines).{warn}{tail}"

    def undo_edit(self, a: Dict[str, Any]) -> str:
        path = self.ws.resolve(a["path"])
        rel = self.ws.rel(path)
        stack = self.state.history.get(rel)
        if not stack:
            return f"Error: no edits to undo for {rel}."
        prev = stack.pop()
        if prev is None:
            path.unlink(missing_ok=True)
            self.state.edited.discard(rel)
            return f"Removed {rel} (it did not exist before)."
        path.write_text(prev, encoding="utf-8")
        self.state.last_edit_step = self.state.seq
        return f"Reverted the last edit to {rel}."

    def git_diff(self, a: Dict[str, Any]) -> str:
        diff = self.ws.diff()
        return self.truncate(diff) if diff.strip() else "No changes yet."

    def run_tests(self, a: Dict[str, Any]) -> str:
        base = self.ws.test_command
        target = str(a.get("target") or "").strip()
        if not base:
            return ("Error: no test command was detected for this repository. Run the tests with bash "
                    "(look at README, Makefile, package.json or CI config for the right command).")
        command = f"{base} {target}".strip()
        if target and target not in self.state.test_targets:
            self.state.test_targets.append(target)
        res = self.ws.shell(command, timeout=self.test_timeout)
        self._record_run(res, "test")
        summary = summarize_tests(res.output)
        head = (f"$ {command}\nexit code: {res.exit_code}" + (" (timed out)" if res.timed_out else "")
                + (f"\nsummary: {summary}" if summary else ""))
        return f"{head}\n{self.truncate(res.output.rstrip() or '(no output)')}"

    def update_plan(self, a: Dict[str, Any]) -> str:
        self.state.plan = str(a["plan"]).strip()
        return "Plan recorded."

    def finish(self, a: Dict[str, Any]) -> str:
        self.state.finish_args = dict(a)
        return "finish requested"

    # ── registry ─────────────────────────────────────────────────────────
    def _build(self) -> List[Tool]:
        S = {"type": "string"}
        I = {"type": "integer"}
        B = {"type": "boolean"}
        return [
            Tool("bash", "Run a shell command in the repository root (bash, no stdin, merged stdout/stderr). "
                 "$SCRATCH is a scratch directory outside the repo for reproduction scripts; the repo root is on "
                 "PYTHONPATH. Long output is truncated. Do not use it to edit files: use edit_file.",
                 _obj({"command": S, "timeout": dict(I, description="seconds, default 300")}, ["command"]), self.bash),
            Tool("read_file", "Read a file with line numbers, 250 lines at a time by default.",
                 _obj({"path": S, "start_line": I, "end_line": I}, ["path"]), self.read_file),
            Tool("search", "Search file contents with a regex (or a literal with fixed_string=true). "
                 "Case-insensitive when the pattern is all lowercase. Returns file:line:text, capped at 50 results.",
                 _obj({"pattern": S, "path": dict(S, description="directory or file to limit the search"),
                       "glob": dict(S, description="file name filter such as *.py"), "fixed_string": B},
                      ["pattern"]), self.search),
            Tool("find_files", "Find files by glob (e.g. **/*config*.py) or by substring of the path.",
                 _obj({"pattern": S}, ["pattern"]), self.find_files),
            Tool("list_dir", "List a directory tree, skipping dependency and build folders.",
                 _obj({"path": S, "depth": I}, []), self.list_dir),
            Tool("edit_file", "Replace old_str with new_str in a file. old_str must match the file exactly and "
                 "be unique (include a few surrounding lines); set replace_all=true to change every occurrence. "
                 "Edits that break Python/JSON/TOML/JS syntax are rejected automatically.",
                 _obj({"path": S, "old_str": S, "new_str": S, "replace_all": B}, ["path", "old_str", "new_str"]),
                 self.edit_file),
            Tool("write_file", "Create a new file or overwrite a small one with the full content. Prefer edit_file "
                 "for changing existing files.", _obj({"path": S, "content": S}, ["path", "content"]), self.write_file),
            Tool("undo_edit", "Revert the most recent edit_file or write_file on a file.",
                 _obj({"path": S}, ["path"]), self.undo_edit),
            Tool("git_diff", "Show all changes made so far, as a unified diff.", _obj({}, []), self.git_diff),
            Tool("run_tests", "Run the project's detected test command, optionally narrowed with a target "
                 "(a test file, test id or pattern appended to the command).",
                 _obj({"target": S}, []), self.run_tests),
            Tool("update_plan", "Record a short checklist of the steps you intend to take. Update it as you go.",
                 _obj({"plan": S}, ["plan"]), self.update_plan),
            Tool("finish", "Call when the fix is complete and verified. summary: what was wrong and what you "
                 "changed. repro_command: the shell command (run from the repo root) that reproduced the bug, "
                 "which must now exit 0, e.g. 'python $SCRATCH/repro.py'.",
                 _obj({"summary": S, "repro_command": S}, ["summary"]), self.finish),
        ]


_PYTEST = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")
_NODE = re.compile(r"^(?:#|ℹ)\s*(pass|fail|tests|skipped)\s+(\d+)", re.M)  # TAP and Node 20+ spec output


def summarize_tests(output: str) -> str:
    counts = {}
    for n, kind in _PYTEST.findall(output[-5000:]):
        counts[kind.rstrip("s") if kind.startswith("error") else kind] = int(n)
    if counts:
        return ", ".join(f"{v} {k}" for k, v in counts.items())
    node = dict((k, int(v)) for k, v in _NODE.findall(output[-5000:]))
    if node:
        return ", ".join(f"{v} {k}" for k, v in node.items())
    go_fail = len(re.findall(r"^--- FAIL", output, re.M))
    go_ok = len(re.findall(r"^ok\s", output, re.M))
    if go_fail or go_ok:
        return f"{go_ok} packages ok, {go_fail} failing tests"
    return ""
