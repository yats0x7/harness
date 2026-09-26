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

import json
import re
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
from .llm import AuthError, ContextOverflow, LLMClient, LLMError, ToolCall, ToolsUnsupported, Usage
from .localize import localize
from .prompts import RETRY, SYSTEM, TASK, TEXT_MODE
from .reviewer import review
from .toolparse import extract_bash_block, extract_text_tool_calls
from .tools import Toolbox, is_test_path, summarize_tests
from .workspace import SKIP_DIRS, Workspace


def failing_tests(output: str) -> Set[str]:
    ids = set(re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", output, re.M))
    ids |= set(re.findall(r"^not ok \d+ - (.+)$", output, re.M))
    ids |= set(re.findall(r"^--- FAIL: (\S+)", output, re.M))
    ids |= set(re.findall(r"^test (\S+) \.\.\. FAILED", output, re.M))
    return {i.strip() for i in ids}


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
    error: str = ""


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
                 cancel: Optional[threading.Event] = None):
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
        # Live progress while a long reply streams in; shown in the UI, not logged.
        self.llm.progress = lambda r, c: self.emit("thinking", record=False, reasoning=r, content=c)

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
                  test_command=self.ws.test_command, language=self.ws.language, run_dir=str(self.ws.run_dir),
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
            attempts.append(att)
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
                           model=ep.model, provider=ep.provider, error=error)
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
        tb = Toolbox(self.ws, self.a.tool_output_chars, self.a.command_timeout, self.a.test_timeout)
        task = TASK.format(root=self.ws.root, issue=self.issue.text.strip(), overview=self.ws.overview(),
                           test_command=self.ws.test_command or "none detected; find it yourself",
                           scratch=self.ws.scratch, max_steps=self.a.max_steps, hints=self.hints)
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
                self.emit("error", text=f"model call failed: {exc}")
                if llm_errors >= 3:
                    att.reason = f"model calls kept failing: {exc}"
                    break
                time.sleep(5)
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
        att.plan = tb.state.plan
        att.tests_modified = sorted(tb.state.tests_modified)
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
            if ctx.no_change_warned:
                att.status, att.summary, att.reason = "no_change", summary, "agent concluded no change is needed"
                return True, "Accepted with no changes."
            ctx.no_change_warned = True
            return False, ("Rejected: the repository has no changes. If the issue truly needs no code change, "
                           "call finish again and explain why in the summary. Otherwise make the fix.")

        if st.last_edit_step > st.last_verify_step and ctx.gate_rejections < 2:
            ctx.gate_rejections += 1
            return False, ("Rejected: files were edited after your last test run, so the fix is unverified. "
                           "Run the reproduction script and run_tests, then call finish again.")

        self.emit("status", text="Verifying the fix")
        ver = self._verify(repro, tb)
        att.verification = ver
        problems = []
        if repro and not ver.get("repro_after_ok"):
            problems.append("The reproduction command still fails on the fixed code:\n"
                            + ver.get("repro_after_tail", ""))
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

        if self.a.review and ctx.review_rounds < self.a.max_review_rounds and not problems:
            ctx.review_rounds += 1
            self.emit("status", text="Reviewer is reading the patch")
            approved, issues, usage = review(self.llm, self.issue.text, diff, _evidence_text(ver, st.tests_modified))
            self._account(usage)
            att.review = {"approved": approved, "problems": issues}
            self.emit("review", approved=approved, problems=issues)
            if not approved:
                return False, ("A reviewer read your patch and asked for changes:\n- " + "\n- ".join(issues)
                               + "\nFix what is valid, re-run the verification, and call finish again. If a point "
                                 "is wrong, say why in the finish summary.")

        att.summary, att.repro_command = summary, repro
        tests_ok = bool(ver.get("tests_ran") and not ver.get("new_failures") and not ver.get("suite_regressed")
                        and (not ver.get("tests_failed_after") or ver.get("preexisting_failures")))
        verified = not problems and bool(ver.get("repro_after_ok") or tests_ok)
        att.status = "verified" if verified else "unverified"
        if not verified:
            att.reason = att.reason or "the harness could not confirm the fix with a reproduction or tests"
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
            if is_test_path(rel) and (self.ws.root / rel).is_file():
                dest = base / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((self.ws.root / rel).read_bytes())
        return base

    def _verify(self, repro: str, tb: Toolbox) -> Dict[str, Any]:
        ws = self.ws
        ver: Dict[str, Any] = {"repro_command": repro}
        timeout = self.a.test_timeout
        base: Optional[Path] = None
        try:
            if repro:
                after = ws.shell(repro, timeout=timeout)
                ver.update(repro_after_exit=after.exit_code, repro_after_ok=after.exit_code == 0,
                           repro_after_tail=_tail(after.output))
                base = self._base_with_new_tests()
                if base:
                    before = ws.shell(repro, timeout=timeout, cwd=base)
                    ver.update(repro_before_exit=before.exit_code, repro_before_tail=_tail(before.output, 800))
                    ver["bug_proven"] = before.exit_code != 0 and after.exit_code == 0
            if ws.test_command:
                targets = self._test_targets(tb)
                if targets is None:
                    ver["tests_note"] = "large test suite and no related tests identified; skipped full rerun"
                else:
                    cmd = f"{ws.test_command} {targets}".strip()
                    after_t = ws.shell(cmd, timeout=timeout)
                    ver.update(tests_ran=True, tests_command=cmd, tests_after_exit=after_t.exit_code,
                               tests_failed_after=after_t.exit_code != 0,
                               tests_after_summary=summarize_tests(after_t.output),
                               tests_after_tail=_tail(after_t.output))
                    if after_t.exit_code != 0:
                        if base is None:
                            base = self._base_with_new_tests()
                        if base:
                            before_t = ws.shell(cmd, timeout=timeout, cwd=base)
                            ver.update(tests_before_exit=before_t.exit_code,
                                       tests_before_summary=summarize_tests(before_t.output))
                            fails_after, fails_before = failing_tests(after_t.output), failing_tests(before_t.output)
                            if fails_after:
                                ver["new_failures"] = sorted(fails_after - fails_before)
                                ver["preexisting_failures"] = sorted(fails_after & fails_before)
                            else:
                                ver["suite_regressed"] = before_t.exit_code == 0
        finally:
            if base:
                ws.drop_worktree(base)
        self.emit("verify", **{k: v for k, v in ver.items() if not k.endswith("_tail")},
                  repro_after_tail=ver.get("repro_after_tail", "")[-600:])
        return ver


def _preview_args(args: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in (args or {}).items():
        s = v if isinstance(v, str) else json.dumps(v)
        out[k] = s if len(s) <= 600 else s[:600] + f"... ({len(s)} chars)"
    return out


def _evidence_text(ver: Dict[str, Any], tests_modified: List[str]) -> str:
    lines = []
    if ver.get("repro_command"):
        lines.append(f"Reproduction command: {ver['repro_command']}")
        if "repro_before_exit" in ver:
            lines.append(f"  exit code on the original code: {ver['repro_before_exit']}")
        lines.append(f"  exit code with the patch: {ver.get('repro_after_exit')}")
    if ver.get("tests_ran"):
        lines.append(f"Tests: {ver.get('tests_command')} -> exit {ver.get('tests_after_exit')} "
                     f"({ver.get('tests_after_summary') or 'no summary'})")
        if ver.get("preexisting_failures"):
            lines.append(f"  failing before and after (pre-existing): {', '.join(ver['preexisting_failures'][:10])}")
    if tests_modified:
        lines.append(f"Existing test files modified by the patch: {', '.join(tests_modified)}")
    return "\n".join(lines) or "No verification evidence."
