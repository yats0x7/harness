"""The agent loop and the checks around it.

One attempt is a single conversation: the model calls tools until it asks to
finish. The harness, not the model, decides whether it is finished:

  1. There must be a change in the repository.
  2. Tests (or the reproduction script) must have run after the last edit.
  3. The harness reruns the reproduction on the original code and on the fix
     (it must fail before and pass after), and reruns the tests on both to
     tell new failures from old ones.
  4. A reviewer pass reads the diff against the issue.

If an attempt ends without a verified fix, a second attempt starts from a
clean tree with a note about what went wrong. The best patch is kept.
"""
from __future__ import annotations

import ast
import json
import re
import shlex
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .config import Config
from .context import Conversation
from .issue import Issue
from .lessons import load as load_lessons, record as record_lesson, render as render_lessons
from .llm import AuthError, ContextOverflow, LLMClient, LLMError, ToolCall, ToolsUnsupported, Usage
from .localize import localize
from .prompts import RETRY, SYSTEM, TASK, TEXT_MODE
from .reviewer import review
from .toolparse import extract_bash_block, extract_text_tool_calls
from .skills import discover as discover_skills, listing as skills_listing
from .tools import Approver, Toolbox, is_test_path, summarize_tests
from .workspace import SKIP_DIRS, Workspace, git


def failing_tests(output: str) -> Set[str]:
    ids = set()
    for match in re.finditer(r"^(FAILED|ERROR)\s+(\S+)", output, re.M):
        kind, test_id = match.groups()
        if kind == "ERROR" and "::" not in test_id:
            continue
        ids.add(test_id)
    ids |= set(re.findall(r"^--- FAIL: (\S+)", output, re.M))
    ids |= set(re.findall(r"^test (\S+) \.\.\. FAILED", output, re.M))
    ids |= set(re.findall(r"^FAIL:\s+(\S+)", output, re.M))
    for match in re.finditer(r"^ERROR:\s+(\S+)", output, re.M):
        if match.group(1).lower() not in {"found", "no", "collecting"}:
            ids.add(match.group(1))
    ids |= set(re.findall(r"^not ok \d+ - (.+)$", output, re.M))
    return {i.strip() for i in ids}


def _has_test_activity(output: str) -> bool:
    """Reject successful commands that collected or executed no tests."""
    if re.search(r"\b[1-9]\d*\s+(?:passed|failed|skipped|xfailed|xpassed)\b", output, re.I):
        return True
    if re.search(r"\bRan\s+[1-9]\d*\s+tests?\b", output, re.I):
        return True
    if re.search(r"\bTests\s+run:\s*[1-9]\d*\b", output, re.I):
        return True
    if re.search(r"^ERROR\s+\S+::\S+", output, re.M):
        return True
    if re.search(r"^ok\s+\S+", output, re.M) or re.search(r"^#\s+(?:pass|fail)\s+[1-9]\d*", output, re.M):
        return True
    return False


def _has_test_failure(output: str) -> bool:
    return bool(failing_tests(output) or re.search(
        r"\b[1-9]\d*\s+failed\b|\bRan\s+[1-9]\d*\s+tests?\b.*\bFAILED\b|"
        r"\bTests\s+run:\s*[1-9]\d*\b.*\b(?:Failures|Errors):\s*[1-9]\d*",
        output, re.I))


def _test_count(output: str) -> Optional[int]:
    """Read counts from common runners so a narrowed run cannot masquerade as a full pass."""
    total = 0
    found = False
    for count, _kind in re.findall(r"\b(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b", output, re.I):
        total += int(count)
        found = True
    if found:
        return total
    match = re.search(r"\bRan\s+(\d+)\s+tests?\b", output, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"^#\s+tests\s+(\d+)", output, re.M)
    return int(match.group(1)) if match else None


def _has_repro_assertion_failure(output: str) -> bool:
    """A nonzero exit alone is not proof; require an explicit behavioral assertion failure."""
    return bool(re.search(
        r"AssertionError|Assertion failed|assertion failed|assert .* failed|"
        r"Expected .{1,160}(?:but got|but was|to equal|to be|not to)|"
        r"actual.{0,80}expected|expected.{0,80}actual",
        output, re.I | re.S))


def _base_has_path(ws: Workspace, rel: str) -> bool:
    return git(ws.root, "cat-file", "-e", f"{ws.base_ref}:{rel}").exit_code == 0


def _scratch_file_argument(argument: str, ws: Workspace) -> Tuple[bool, Optional[Path]]:
    candidate = Path(argument.replace("$SCRATCH", str(ws.scratch)).replace("$REPO", str(ws.root)))
    if not candidate.is_absolute():
        candidate = ws.root / candidate
    try:
        candidate.absolute().relative_to(ws.scratch.absolute())
    except ValueError:
        return True, None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(ws.scratch.resolve())
    except (OSError, ValueError):
        return False, None
    return True, resolved if resolved.is_file() else None


def _repro_is_independent(command: str, ws: Workspace, changed_tests: List[str]) -> bool:
    """A scratch reproduction must not delegate proof to a changed test or test runner."""
    normalized = command.replace("\\", "/")
    if _looks_like_test_command(normalized) or any(path in normalized for path in changed_tests):
        return False
    metadata_probe = re.compile(
        r"\btest\s+-[efd]\b|\b(?:ls|stat|scandir|listdir|iterdir|walk)\b|"
        r"\bcat\s+[^;&|]*>\s*(?:/dev/null|NUL)\b|"
        r"\bgit\s+(?:status|diff|ls-files)\b|"
        r"\bread_(?:bytes|text)\s*\(|\bopen\s*\([^)]*\)\s*\.\s*read(?:line|lines)?\s*\(|"
        r"\b(?:exists|is_file|is_dir|existsSync|access|stat|lstat|listdir|scandir|walk)\s*\(|"
        r"\b(?:os|Path)\.(?:path\.)?(?:exists|isfile|isdir|access|listdir|scandir|walk)\s*\(|\bF_OK\b",
        re.I)
    if metadata_probe.search(normalized):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    test_runner = re.compile(
        r"\b(pytest|unittest|jest|vitest|mocha|go\s+test|cargo\s+test|npm\s+test|yarn\s+test|pnpm\s+test)\b",
        re.I)
    for argument in parts:
        safe, candidate = _scratch_file_argument(argument, ws)
        if not safe:
            return False
        if candidate is None:
            continue
        try:
            if candidate.stat().st_size > 100_000:
                return False
            source = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        compact_source = re.sub(r"[^a-z0-9]", "", source.lower())
        assembled_runner = any(runner in compact_source for runner in
                               ("pytest", "unittest", "jest", "vitest", "mocha"))
        numeric_runner = False
        try:
            tree = ast.parse(source)

            def constant_text(node):
                if isinstance(node, ast.Constant):
                    return node.value if isinstance(node.value, (str, int)) else None
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                    left, right = constant_text(node.left), constant_text(node.right)
                    if isinstance(left, int) and isinstance(right, int):
                        return left + right
                    if isinstance(left, (str, int)) and isinstance(right, (str, int)):
                        return str(left) + str(right)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "chr" and node.args:
                    value = constant_text(node.args[0])
                    return chr(value) if isinstance(value, int) and 32 <= value <= 126 else None
                if isinstance(node, (ast.List, ast.Tuple)):
                    values = [constant_text(item) for item in node.elts]
                    if values and all(isinstance(value, int) and 32 <= value <= 126 for value in values):
                        return "".join(chr(value) for value in values)
                    if values and all(isinstance(value, str) for value in values):
                        return "".join(values)
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "join" and node.args):
                    separator = constant_text(node.func.value)
                    values = constant_text(node.args[0])
                    if isinstance(separator, str) and isinstance(values, str):
                        return separator.join(values)
                return None

            for node in ast.walk(tree):
                decoded = constant_text(node)
                if isinstance(decoded, str) and any(
                        runner in decoded.lower() for runner in ("pytest", "unittest", "jest", "vitest", "mocha")):
                    numeric_runner = True
                    break
                if isinstance(node, (ast.List, ast.Tuple)) and all(
                        isinstance(item, ast.Constant) and isinstance(item.value, int)
                        and 32 <= item.value <= 126 for item in node.elts):
                    decoded = "".join(chr(item.value) for item in node.elts).lower()
                    if any(runner in decoded for runner in ("pytest", "unittest", "jest", "vitest", "mocha")):
                        numeric_runner = True
                        break
        except SyntaxError:
            pass
        if (test_runner.search(source) or assembled_runner or metadata_probe.search(source)
                or numeric_runner or any(path in source.replace("\\", "/") for path in changed_tests)):
            return False
    return True


def _tail(text: str, n: int = 1500) -> str:
    text = text.rstrip()
    return text if len(text) <= n else "..." + text[-n:]


@dataclass
class Attempt:
    number: int
    status: str = "unfinished"  # verified | unverified | no_change | unfinished | error
    patch: str = ""
    summary: str = ""
    repro_command: str = ""
    verification: Dict[str, Any] = field(default_factory=dict)
    review: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    steps: int = 0
    tests_modified: List[str] = field(default_factory=list)
    plan: str = ""
    changed_files: List[str] = field(default_factory=list)

    @property
    def score(self) -> Tuple[int, int, int, int]:
        v = self.verification
        return (1 if self.status == "verified" else 0,
                1 if v.get("repro_after_ok") else 0,
                -len(v.get("new_failures") or []),
                1 if self.patch.strip() else 0)


@dataclass
class RunResult:
    status: str
    attempts: List[Attempt]
    best: Optional[Attempt]
    usage: Usage
    elapsed: float
    run_dir: Path
    model: str
    provider: str
    cost: Optional[float] = None
    error: str = ""
    tournament: Dict[str, Any] = field(default_factory=dict)


class _Ctx:
    """Per-attempt bookkeeping for the finish gate."""

    def __init__(self):
        self.no_change_warned = False
        self.gate_rejections = 0
        self.verify_rejections = 0
        self.review_rounds = 0


class Agent:
    def __init__(self, cfg: Config, llm: LLMClient, ws: Workspace, issue: Issue,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 cancel: Optional[threading.Event] = None, approver: Optional[Approver] = None):
        self.cfg = cfg
        self.a = cfg.agent
        self.llm = llm
        self.ws = ws
        self.issue = issue
        self.on_event = on_event
        self.cancel = cancel or threading.Event()
        self.text_mode = cfg.agent.tool_mode == "text"
        self.usage = Usage()
        self.started = time.time()
        self._traj = open(ws.run_dir / "trajectory.jsonl", "a", encoding="utf-8")
        self._lock = threading.Lock()
        self.hints = ""
        self.approver = approver
        self.skills = discover_skills(ws.root)
        self.lessons = load_lessons(ws.run_dir.parent, issue.kind)
        # Live progress while a long reply streams in; shown in the UI, not logged.
        self.llm.progress = lambda r, c: self.emit("thinking", record=False, reasoning=r, content=c)
        self.llm.on_switch = lambda old, new: self.emit(
            "warning", text=f"{old} ran out of its daily quota; continuing with {new} (AI_FALLBACK_MODELS)")

    # ── events ───────────────────────────────────────────────────────────
    def emit(self, kind: str, record: bool = True, **data: Any) -> None:
        event = {"t": round(time.time() - self.started, 2), "kind": kind, **data}
        if record:
            with self._lock:
                if not self._traj.closed:
                    self._traj.write(json.dumps(event, default=str) + "\n")
                    self._traj.flush()
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

    def cost(self) -> Optional[float]:
        price = self.cfg.price_for(self.llm.endpoint.model)
        if not price:
            return None
        fresh = max(self.usage.prompt - self.usage.cached, 0)
        return (fresh * price.get("input", 0) + self.usage.cached * price.get("cached", price.get("input", 0))
                + self.usage.completion * price.get("output", 0)) / 1_000_000

    def _account(self, usage: Usage) -> None:
        self.usage.add(usage)
        self.emit("usage", prompt=self.usage.prompt, completion=self.usage.completion, cached=self.usage.cached,
                  requests=self.usage.requests, cost=self.cost(), last_prompt=usage.prompt,
                  last_cached=usage.cached)

    # ── top level ────────────────────────────────────────────────────────
    def run(self) -> RunResult:
        ep = self.llm.endpoint
        self.emit("start", issue=self.issue.short, repo=str(self.ws.root), provider=ep.provider, model=ep.model,
                  skills=sorted(self.skills), approval=self.cfg.agent.approval,
                  task_type=self.issue.kind, test_command=self.ws.test_command, language=self.ws.language,
                  run_dir=str(self.ws.run_dir),
                  notes=self.ws.notes)
        self.emit("status", text="Finding likely files")
        self.hints = localize(self.ws, self.issue.text)
        self.emit("hints", text=self.hints)

        attempts: List[Attempt] = []
        retry_note = ""
        error = ""
        for n in range(1, max(1, self.a.max_attempts) + 1):
            if n > 1:
                self.ws.reset_to_base()
            try:
                att = self._attempt(n, retry_note)
            except AuthError as exc:
                error = str(exc)
                self.emit("error", text=error)
                break
            except KeyboardInterrupt:
                # Interrupted (Ctrl-C or SIGTERM): keep whatever was done so the report still gets written.
                error = "interrupted"
                self.emit("error", text="run interrupted; writing the report for the work so far")
                att = Attempt(number=n, patch=self.ws.diff(), reason="interrupted")
                attempts.append(att)
                break
            if self.cancel.is_set() and att.status not in ("verified", "no_change"):
                att.reason = "cancelled"
            attempts.append(att)
            if att.status not in ("verified", "no_change"):
                record_lesson(self.ws.run_dir.parent, self.issue, att)
            self.emit("attempt_done", number=n, status=att.status, reason=att.reason)
            if att.status in ("verified", "no_change") or self.cancel.is_set():
                break
            if self.usage.total > self.a.max_total_tokens * 0.6:
                self.emit("status", text="Token budget too low for another attempt")
                break
            if n < self.a.max_attempts:
                retry_note = RETRY.format(summary=att.summary or att.plan or "(no summary)",
                                          reason=att.reason or "it did not pass verification")
                self.emit("status", text=f"Attempt {n} unverified; starting attempt {n + 1} from a clean tree")

        best = max(attempts, key=lambda a: a.score) if attempts else None
        if best is not None and attempts and best is not attempts[-1]:
            self.ws.reset_to_base()
            if not self.ws.apply_patch(best.patch):
                self.emit("error", text="could not re-apply the best patch; leaving the last attempt in place")
                best = attempts[-1]
        status = best.status if best else "error"
        if best and status == "unfinished" and best.patch.strip():
            status = "unverified"
        result = RunResult(status=status, attempts=attempts, best=best, usage=self.usage,
                           elapsed=time.time() - self.started, run_dir=self.ws.run_dir,
                           model=ep.model, provider=ep.provider, cost=self.cost(), error=error)
        self.emit("done", status=status, patch=best.patch if best else "", summary=best.summary if best else "",
                  verification=best.verification if best else {}, elapsed=result.elapsed, cost=self.cost())
        self._traj.close()
        return result

    # ── one attempt ──────────────────────────────────────────────────────
    def _system(self, tb: Toolbox) -> str:
        if not self.text_mode:
            return SYSTEM
        listing = "\n".join(f"- {t.name}({', '.join(t.parameters.get('properties', {}))}): {t.description}"
                            for t in tb.tools.values())
        return SYSTEM + TEXT_MODE.format(tools=listing)

    def _state_note(self, tb: Toolbox) -> str:
        st = tb.state
        last = st.runs[-1] if st.runs else None
        return (f"Plan:\n{st.plan or '(none recorded)'}\n"
                f"Files edited so far: {', '.join(sorted(st.edited)) or 'none'}\n"
                f"Last command: {last['command'] + ' -> exit ' + str(last['exit_code']) if last else 'none'}")

    def _attempt(self, number: int, retry_note: str) -> Attempt:
        att = Attempt(number=number)
        tb = Toolbox(self.ws, self.a.tool_output_chars, self.a.command_timeout, self.a.test_timeout,
                     skills=self.skills, approver=self.approver)
        acceptance = "\n".join(f"- {item}" for item in self.issue.task.acceptance) or "(derive concrete checks from the request)"
        constraints = "\n".join(f"- {item}" for item in self.issue.task.constraints) or "(none stated)"
        task = TASK.format(root=self.ws.root, task_kind=self.issue.task.kind, issue=self.issue.text.strip(),
                           acceptance=acceptance, constraints=constraints, lessons=render_lessons(self.lessons),
                           overview=self.ws.overview(),
                           test_command=self.ws.test_command or "none detected; find it yourself",
                           scratch=self.ws.scratch, max_steps=self.a.max_steps, hints=self.hints,
                           skills=skills_listing(self.skills))
        if retry_note:
            task += "\n\n" + retry_note
        conv = Conversation(self._system(tb), task, self.a.context_window, self.a.compact_at,
                            self.a.keep_recent_messages)
        ctx = _Ctx()
        recent: deque = deque(maxlen=8)
        no_call_streak = 0
        llm_errors = 0
        overflows = 0
        self.emit("attempt", number=number)

        step = 0
        while step < self.a.max_steps:
            if self.cancel.is_set():
                att.reason = "cancelled"
                break
            step += 1
            att.steps = step
            tb.state.step = step
            if self.usage.total > self.a.max_total_tokens:
                att.reason = "token budget exhausted"
                break
            note = conv.maybe_compact(state_note=self._state_note(tb))
            if note:
                self.emit("compact", text=note)

            self.emit("step", number=step, attempt=number, context=conv.size())
            try:
                reply = self.llm.chat(conv.messages, tools=None if self.text_mode else tb.schemas())
            except ContextOverflow:
                overflows += 1
                if overflows > 3:
                    att.reason = "the prompt stayed too long for the model even after compaction"
                    break
                self.emit("compact", text="provider said the prompt is too long; compacting")
                conv.maybe_compact(force=True, state_note=self._state_note(tb))
                step -= 1
                continue
            except ToolsUnsupported:
                if self.text_mode:
                    raise
                self.text_mode = True
                conv.messages[0]["content"] = self._system(tb)
                self.emit("warning", text="endpoint rejected native tools; switched to text tool calls")
                step -= 1
                continue
            except AuthError:
                raise
            except LLMError as exc:
                llm_errors += 1
                rate_limited = "429" in str(exc) or "rate" in str(exc).lower()
                # A busy provider is not the model failing: wait it out (about 15 minutes) instead of
                # burning the attempt. Other errors get three strikes.
                limit = 8 if rate_limited else 3
                self.emit("error", text=f"model call failed ({llm_errors}/{limit}): {str(exc)[:200]}")
                if llm_errors >= limit:
                    att.reason = f"model calls kept failing: {str(exc)[:200]}"
                    break
                time.sleep(60 if rate_limited else 5)
                step -= 1
                continue
            llm_errors = 0
            overflows = 0
            conv.note_usage(reply.usage.prompt)
            self._account(reply.usage)

            calls = list(reply.tool_calls)
            message = reply.message
            if not calls:
                parsed, cleaned = extract_text_tool_calls(reply.content, tb.parameter_schemas())
                if not parsed and self.text_mode:
                    cmd = extract_bash_block(reply.content)
                    if cmd:
                        parsed = [("bash", {"command": cmd}, None)]
                if parsed:
                    calls = [ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=n, arguments=a, error=e)
                             for n, a, e in parsed]
                    if not self.text_mode:
                        message = dict(message, content=cleaned, tool_calls=[
                            {"id": c.id, "type": "function",
                             "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in calls])
                        self.emit("warning", text=f"recovered {len(calls)} tool call(s) written as text")
            conv.add(message)
            self.emit("assistant", text=reply.content if not calls else (message.get("content") or ""),
                      reasoning=reply.reasoning[-1500:] if reply.reasoning else "")

            if not calls:
                no_call_streak += 1
                if reply.finish_reason == "length":
                    nudge = "Your reply was cut off. Continue with a single tool call and keep it short."
                elif no_call_streak >= 3:
                    calls = [ToolCall(id="implicit_finish", name="finish",
                                      arguments={"summary": reply.content[:2000]})]
                    nudge = ""
                else:
                    nudge = ("Continue by calling a tool. When the fix is made and verified, call finish."
                             if not self.text_mode else
                             "Continue with one tool call in the <tool_call> format. Call finish when done.")
                if nudge:
                    conv.add({"role": "user", "content": nudge})
                    continue
                if not self.text_mode:  # implicit finish has no matching assistant tool call
                    conv.messages[-1] = dict(conv.messages[-1], tool_calls=[
                        {"id": "implicit_finish", "type": "function",
                         "function": {"name": "finish", "arguments": json.dumps(calls[0].arguments)}}])
            no_call_streak = 0

            results: List[Tuple[ToolCall, str]] = []
            finish_call = None
            for call in calls:
                if call.name == "finish":
                    finish_call = call
                    continue
                self.emit("tool_call", name=call.name, args=_preview_args(call.arguments))
                if call.error:
                    out = f"Error: {call.error}. Resend the call with valid JSON arguments."
                else:
                    out = tb.call(call.name, call.arguments)
                key = call.name + json.dumps(call.arguments, sort_keys=True)
                recent.append(key)
                repeats = recent.count(key)
                if repeats >= 3:
                    diff = self.ws.diff()
                    out += (f"\n\n[Harness: you have made this exact call {repeats} times and it will not change the "
                            "result. Stop repeating it. Here is the current state of your changes:\n"
                            + (diff[:3000] if diff.strip() else "(no changes yet)")
                            + "\nDecide the next different step: run the reproduction and tests, read other code, "
                              "or call finish if the fix is verified.]")
                self.emit("tool_result", name=call.name, text=out[:4000], ok=not out.startswith("Error"))
                if call.name == "update_plan":
                    self.emit("plan", text=tb.state.plan)
                results.append((call, out))

            accepted = False
            if finish_call is not None:
                self.emit("tool_call", name="finish", args=_preview_args(finish_call.arguments))
                accepted, out = self._try_finish(finish_call, tb, ctx, att)
                self.emit("tool_result", name="finish", text=out[:4000], ok=accepted)
                results.append((finish_call, out))

            if self.text_mode:
                body = "\n\n".join(f"<tool_result name=\"{c.name}\">\n{o}\n</tool_result>" for c, o in results)
                conv.add({"role": "user", "content": body})
            else:
                for c, o in results:
                    conv.add({"role": "tool", "tool_call_id": c.id, "content": o})
            if accepted:
                break
            if step == self.a.max_steps - 5:
                conv.add({"role": "user", "content": "[Harness: 5 turns left. Verify your fix and call finish.]"})
        else:
            att.reason = att.reason or f"reached the step limit ({self.a.max_steps})"

        att.patch = self.ws.diff()
        att.changed_files = self.ws.changed_files()
        att.plan = tb.state.plan
        att.tests_modified = sorted(path for path in att.changed_files if is_test_path(path))
        if att.status == "unfinished":
            if not att.verification and att.patch.strip():
                self.emit("status", text="Attempt ended without finish; verifying what is there")
                att.verification = self._verify("", tb)
            if not att.reason:
                att.reason = "ended without calling finish"
        return att

    # ── finishing ────────────────────────────────────────────────────────
    def _try_finish(self, call: ToolCall, tb: Toolbox, ctx: _Ctx, att: Attempt) -> Tuple[bool, str]:
        args = call.arguments or {}
        summary = str(args.get("summary") or "").strip()
        repro = str(args.get("repro_command") or "").strip()
        diff = self.ws.diff()
        st = tb.state

        if not diff.strip():
            # "Nothing to fix" needs the same evidence as a fix: a reproduction
            # that passes on the untouched code. A model that believes it edited
            # files when every edit failed must not be able to finish this way.
            proof = None
            if repro:
                res = self.ws.shell(repro, timeout=self.a.test_timeout)
                proof = res.exit_code == 0
            if proof:
                att.status, att.summary = "no_change", summary
                att.reason = "the reproduction passes on the unchanged code"
                att.verification = {"repro_command": repro, "repro_after_exit": 0, "repro_after_ok": True}
                att.verification["decision_reason"] = "reproduction passes without changes; no patch was needed"
                return True, "Accepted: the reproduction passes without changes."
            ctx.no_change_warned = True
            if ctx.gate_rejections >= 3:
                att.reason = "finished without changes and without proof that none are needed"
                return True, "Accepted as unresolved."
            ctx.gate_rejections += 1
            return False, ("Rejected: the repository has no changes. If you made edits, they did not take effect: "
                           "check the results of your edit_file calls, they may have returned errors. If the issue "
                           "truly needs no change, call finish with a repro_command that passes on the current code.")

        if st.last_edit_step > st.last_verify_step and ctx.gate_rejections < 2:
            ctx.gate_rejections += 1
            return False, ("Rejected: files were edited after your last test run, so the fix is unverified. "
                           "Run the reproduction script and run_tests, then call finish again.")

        self.emit("status", text="Verifying the fix")
        ver = self._verify(repro, tb)
        att.verification = ver
        problems = []
        if repro and not ver.get("repro_after_ok"):
            hint = ("\nEither the fix is incomplete, or the reproduction asserts the wrong behaviour. Re-read the "
                    "expected behaviour in the issue and check which one it is.")
            if ver.get("repro_before_exit") == ver.get("repro_after_exit"):
                hint += " It fails the same way before and after your change, which suggests the script itself is wrong."
            problems.append("The reproduction command still fails on the fixed code:\n"
                            + ver.get("repro_after_tail", "") + hint)
        if ver.get("new_failures"):
            problems.append("These tests pass on the original code but fail with your change: "
                            + ", ".join(ver["new_failures"][:15]))
        elif ver.get("suite_regressed"):
            problems.append("The test suite passed on the original code but fails with your change:\n"
                            + ver.get("tests_after_tail", ""))
        if problems and ctx.verify_rejections < 2:
            ctx.verify_rejections += 1
            att.reason = "; ".join(p.splitlines()[0] for p in problems)
            return False, "Rejected by the harness's own verification:\n- " + "\n- ".join(problems)

        att.review = None
        if self.a.review and ctx.review_rounds < self.a.max_review_rounds and not problems:
            ctx.review_rounds += 1
            self.emit("status", text="Reviewer is reading the patch")
            approved, issues, usage = review(
                self.llm, self.issue.text, diff, _evidence_text(ver, st.tests_modified, self.ws))
            self._account(usage)
            att.review = {"approved": approved, "problems": issues}
            self.emit("review", approved=approved, problems=issues)
            if not approved:
                return False, ("A reviewer read your patch and asked for changes:\n- " + "\n- ".join(issues)
                               + "\nFix what is valid, re-run the verification, and call finish again. If a point "
                                 "is wrong, say why in the finish summary.")

        att.summary, att.repro_command = summary, repro
        tests_ok = bool(ver.get("tests_evidence"))
        repro_ok = bool(ver.get("bug_proven") and ver.get("repro_independent"))
        reviewer_ok = (bool(att.review and att.review.get("approved")) if self.a.review else tests_ok)
        verified = not problems and bool(repro_ok or tests_ok) and reviewer_ok
        if repro_ok:
            ver["decision_reason"] = "independent reproduction failed on original code and passed after the change"
        elif tests_ok:
            ver["decision_reason"] = "unchanged test suite failed on original code and passed after the change"
        else:
            ver["decision_reason"] = ver.get("tests_evidence_reason") or "no strong before/after verification evidence"
        att.status = "verified" if verified else "unverified"
        if not verified:
            att.reason = att.reason or (ver["decision_reason"] if reviewer_ok else
                                        "independent review did not approve this exact patch")
        return True, "Accepted." if verified else "Accepted, but the fix is not verified."

    def _test_targets(self, tb: Toolbox) -> Optional[str]:
        """Which tests the harness reruns: all of them in small repos, related ones in big ones."""
        tests = [f for f in self.ws.tracked_files() if is_test_path(f) and not any(p in SKIP_DIRS for p in Path(f).parts)]
        if len(tests) <= 80:
            return ""
        targets = list(tb.state.test_targets)
        for changed in self.ws.changed_files():
            stem = Path(changed).stem.replace("test_", "")
            targets += [t for t in tests if stem and stem in Path(t).stem][:5]
        targets = list(dict.fromkeys(t for t in targets if t))
        return " ".join(targets[:20]) if targets else None

    def _base_with_new_tests(self) -> Optional[Path]:
        """A checkout of the original code, plus the agent's test changes.

        Running the agent's own tests against the unfixed code is what makes
        "fails before, passes after" mean something: a test that only exists
        after the change would otherwise "fail" before for the wrong reason.
        """
        base = self.ws.base_worktree()
        if base is None:
            return None
        for rel in self.ws.changed_files():
            if is_test_path(rel) and not _base_has_path(self.ws, rel) and (self.ws.root / rel).is_file():
                dest = base / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((self.ws.root / rel).read_bytes())
        return base

    def _verify(self, repro: str, tb: Toolbox) -> Dict[str, Any]:
        ws = self.ws
        changed_tests = sorted(path for path in ws.changed_files() if is_test_path(path))
        preexisting_test_changes = sorted(path for path in changed_tests if _base_has_path(ws, path))
        added_test_files = sorted(path for path in changed_tests if not _base_has_path(ws, path)
                                  and (ws.root / path).is_file())
        ver: Dict[str, Any] = {"repro_command": repro, "changed_test_files": changed_tests,
                               "modified_existing_tests": preexisting_test_changes,
                               "added_test_files": added_test_files}
        timeout = self.a.test_timeout
        base: Optional[Path] = None
        try:
            if repro or ws.test_command:
                self.emit("status", text="Preparing original-code baseline")
                base = self._base_with_new_tests()
            if repro:
                self.emit("status", text="Running reproduction on changed code")
                after = ws.shell(repro, timeout=timeout)
                ver.update(repro_after_exit=after.exit_code, repro_after_ok=after.exit_code == 0,
                           repro_after_tail=_tail(after.output))
                if base:
                    before = ws.shell(repro, timeout=timeout, cwd=base)
                    ver.update(repro_before_exit=before.exit_code, repro_before_timed_out=before.timed_out,
                               repro_before_tail=_tail(before.output, 800))
                    # 126/127 mean "could not execute" / "command not found": the check did not run,
                    # so it proves nothing about the bug.
                    ran_before = before.exit_code not in (126, 127, -9)
                    ver["bug_proven"] = bool(
                        ran_before and _has_repro_assertion_failure(before.output) and after.exit_code == 0)
                    ver["repro_independent"] = bool(
                        ver["bug_proven"] and _repro_is_independent(repro, ws, changed_tests))
                    if not ran_before:
                        ver["repro_before_note"] = "the reproduction could not run on the original code"
                    elif before.exit_code != 0 and not ver["bug_proven"]:
                        ver["repro_before_note"] = "the original-code command failed without a recognized assertion failure"
            if ws.test_command:
                targets = self._test_targets(tb)
                if targets is None:
                    ver["tests_note"] = "large test suite and no related tests identified; skipped full rerun"
                else:
                    cmd = f"{ws.test_command} {targets}".strip()
                    self.emit("status", text="Running tests on changed code")
                    after_t = ws.shell(cmd, timeout=timeout)
                    ver.update(tests_ran=True, tests_command=cmd, tests_after_exit=after_t.exit_code,
                               tests_failed_after=after_t.exit_code != 0,
                               tests_after_summary=summarize_tests(after_t.output),
                               tests_after_activity=_has_test_activity(after_t.output),
                               tests_after_tail=_tail(after_t.output))
                    if base:
                        self.emit("status", text="Running tests on original code")
                        before_t = ws.shell(cmd, timeout=timeout, cwd=base)
                        ver.update(tests_before_exit=before_t.exit_code,
                                   tests_before_timed_out=before_t.timed_out,
                                   tests_before_summary=summarize_tests(before_t.output),
                                   tests_before_activity=_has_test_activity(before_t.output),
                                   tests_before_tail=_tail(before_t.output))
                        fails_after, fails_before = failing_tests(after_t.output), failing_tests(before_t.output)
                        ver["new_failures"] = sorted(fails_after - fails_before)
                        ver["preexisting_failures"] = sorted(fails_after & fails_before)
                        ver["suite_regressed"] = before_t.exit_code == 0 and after_t.exit_code != 0
                        passed_after = after_t.exit_code == 0 and _has_test_activity(after_t.output)
                        failed_before = (not before_t.timed_out and before_t.exit_code != 0
                                         and _has_test_activity(before_t.output)
                                         and _has_test_failure(before_t.output))
                        count_before, count_after = _test_count(before_t.output), _test_count(after_t.output)
                        coverage_drop = count_before is not None and count_after is not None and count_after < count_before
                        ver.update(tests_before_count=count_before, tests_after_count=count_after,
                                   tests_coverage_drop=coverage_drop)
                        ver["tests_evidence"] = bool(passed_after and failed_before and not changed_tests
                                                     and not coverage_drop)
                        if changed_tests:
                            ver["tests_evidence_reason"] = "test files changed; suite result cannot be the only verification evidence"
                        elif not passed_after:
                            ver["tests_evidence_reason"] = "post-change test run failed or did not execute tests"
                        elif before_t.timed_out:
                            ver["tests_evidence_reason"] = "original-code test run timed out"
                        elif not failed_before:
                            ver["tests_evidence_reason"] = "original-code test run did not show a test failure"
                        elif coverage_drop:
                            ver["tests_evidence_reason"] = "fewer tests ran after the change"
                    else:
                        ver["tests_evidence"] = False
                        ver["tests_evidence_reason"] = "original-code test run was unavailable"
        finally:
            if base:
                ws.drop_worktree(base)
        self.emit("verify", **{k: v for k, v in ver.items() if not k.endswith("_tail")},
                  repro_after_tail=ver.get("repro_after_tail", "")[-600:])
        return ver


def _looks_like_test_command(command: str) -> bool:
    return bool(re.search(
        r"\b(pytest|unittest|jest|vitest|mocha|go\s+test|cargo\s+test|npm\s+test|"
        r"yarn\s+test|pnpm\s+test|mvn\s+.*test|gradle\s+.*test)\b", command, re.I))


def _preview_args(args: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in (args or {}).items():
        s = v if isinstance(v, str) else json.dumps(v)
        out[k] = s if len(s) <= 600 else s[:600] + f"... ({len(s)} chars)"
    return out


def _evidence_text(ver: Dict[str, Any], tests_modified: List[str], ws: Optional[Workspace] = None) -> str:
    lines = []
    if ver.get("repro_command"):
        lines.append(f"Reproduction command: {ver['repro_command']}")
        if "repro_before_exit" in ver:
            lines.append(f"  exit code on the original code: {ver['repro_before_exit']}")
        lines.append(f"  exit code with the patch: {ver.get('repro_after_exit')}")
        if ver.get("repro_before_tail"):
            lines.append(f"  original-code output: {ver['repro_before_tail'][-1000:]}")
        if ver.get("repro_after_tail"):
            lines.append(f"  patched-code output: {ver['repro_after_tail'][-1000:]}")
        if ws:
            try:
                parts = shlex.split(ver["repro_command"])
            except ValueError:
                parts = []
            for argument in parts:
                safe, candidate = _scratch_file_argument(argument, ws)
                if not safe:
                    continue
                if candidate is not None:
                    source = candidate.read_bytes()[:10_000].decode("utf-8", errors="replace")
                    lines.append("  reproduction source (review for task relevance):\n" + _tail(source, 2500))
                    break
    if ver.get("tests_ran"):
        lines.append(f"Tests: {ver.get('tests_command')} -> exit {ver.get('tests_after_exit')} "
                     f"({ver.get('tests_after_summary') or 'no summary'})")
        if ver.get("preexisting_failures"):
            lines.append(f"  failing before and after (pre-existing): {', '.join(ver['preexisting_failures'][:10])}")
    if tests_modified:
        lines.append(f"Existing test files modified by the patch: {', '.join(tests_modified)}")
    return "\n".join(lines) or "No verification evidence."
