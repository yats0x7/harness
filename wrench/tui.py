"""Terminal UI: `make run`.

A form to enter the repository and the issue, then a live view of the run: the
agent's steps on the left; the plan, a cost meter and the verification evidence
on the right. The same event stream drives headless mode and replay.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Input, Label, RichLog, Static, TextArea

from .config import Config
from .issue import load_issue
from .llm import AuthError, LLMError, LLMClient, resolve_endpoint

_VERB = {"bash": "$", "read_file": "read", "search": "search", "find_files": "find", "list_dir": "ls",
         "edit_file": "edit", "write_file": "write", "undo_edit": "undo", "git_diff": "diff",
         "run_tests": "test", "update_plan": "plan", "finish": "finish"}
_STATUS_STYLE = {"verified": "bold green", "no_change": "bold yellow", "unverified": "bold red",
                 "unfinished": "bold red", "error": "bold red"}


def _arg(name: str, args: Dict[str, Any]) -> str:
    if name == "bash":
        return args.get("command", "")
    if name in ("read_file", "edit_file", "write_file", "undo_edit"):
        rng = f" :{args['start_line']}" if name == "read_file" and args.get("start_line") else ""
        return f"{args.get('path', '')}{rng}"
    if name == "search":
        return repr(args.get("pattern", "")) + (f" in {args['path']}" if args.get("path") else "")
    if name == "run_tests":
        return args.get("target", "") or "(all)"
    if name == "finish":
        return args.get("summary", "")[:160]
    return " ".join(f"{k}={v}" for k, v in args.items())[:160]


class WrenchApp(App):
    TITLE = "wrench"
    CSS = """
    Screen { background: #0f1419; }
    #form { padding: 1 2; height: 1fr; }
    #form Label { color: #8b98a8; margin-top: 1; }
    #title { color: #e6edf3; text-style: bold; }
    #subtitle { color: #8b98a8; margin-bottom: 1; }
    #model-line { color: #8b98a8; margin-top: 1; }
    #repo { margin-bottom: 0; }
    #issue { height: 1fr; min-height: 8; }
    #start { margin-top: 1; width: 24; }
    #run { height: 1fr; display: none; }
    #topbar { height: 3; padding: 0 1; background: #161d27; color: #e6edf3; border-bottom: solid #2a3441; }
    #main { height: 1fr; }
    #log { width: 3fr; border-right: solid #2a3441; padding: 0 1; background: #0f1419; }
    #side { width: 1fr; min-width: 38; padding: 0 1; background: #121922; }
    .panel-title { color: #7c9bff; text-style: bold; margin-top: 1; }
    #meter, #plan, #files, #evidence { color: #c9d1d9; }
    """
    BINDINGS = [
        Binding("ctrl+s", "start", "Start", show=True),
        Binding("d", "diff", "Show diff", show=True),
        Binding("c", "cancel", "Cancel run", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, cfg: Config, repo: str = "", issue_text: str = "", autostart: bool = False,
                 replay_dir: Optional[Path] = None):
        super().__init__()
        self.cfg = cfg
        self.initial_repo = repo
        self.initial_issue = issue_text
        self.autostart = autostart
        self.replay_dir = replay_dir
        self.cancel_event = threading.Event()
        self.running = False
        self.started_at = 0.0
        self.llm: Optional[LLMClient] = None
        self.last_patch = ""
        self.usage: Dict[str, Any] = {}
        self.step = 0
        self.attempt = 1
        self.status_text = "Idle"
        self.files: set = set()
        self.wrench_exit = 0

    # ── layout ───────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Static("wrench", id="title")
            yield Static("Give it a repository and an issue. It finds the code, reproduces the bug, fixes it, "
                         "and proves the fix before it stops.", id="subtitle")
            yield Label("Repository: a local path or a git URL (leave empty if the issue is a GitHub issue URL)")
            yield Input(value=self.initial_repo, placeholder="/path/to/repo  or  https://github.com/owner/repo",
                        id="repo")
            yield Label("Issue: paste the text, a GitHub issue URL, or @path/to/issue.md")
            yield TextArea(self.initial_issue, id="issue")
            yield Static("Model: checking AI_API_KEY...", id="model-line")
            yield Button("Start  (ctrl+s)", id="start", variant="primary")
        with Vertical(id="run"):
            yield Static("", id="topbar")
            with Horizontal(id="main"):
                yield RichLog(id="log", wrap=True, markup=False, highlight=False)
                with VerticalScroll(id="side"):
                    yield Static("Status", classes="panel-title")
                    yield Static("", id="status")
                    yield Static("Cost meter", classes="panel-title")
                    yield Static("", id="meter")
                    yield Static("Plan", classes="panel-title")
                    yield Static("(waiting for the agent)", id="plan")
                    yield Static("Files changed", classes="panel-title")
                    yield Static("none yet", id="files")
                    yield Static("Evidence", classes="panel-title")
                    yield Static("not verified yet", id="evidence")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh_meter)
        if self.replay_dir:
            self._show_run()
            self._replay(self.replay_dir)
            return
        self._connect()

    # ── model connection (runs off the UI thread) ────────────────────────
    @work(thread=True, exclusive=True, group="connect")
    def _connect(self) -> None:
        try:
            llm = LLMClient(resolve_endpoint(self.cfg), self.cfg.model)
            self.llm = llm
            msg = f"Model: [b]{escape(llm.endpoint.model)}[/b] via {llm.endpoint.provider}"
        except (AuthError, LLMError) as exc:
            msg = f"[red]Model: {escape(str(exc))}[/red]"
        self.call_from_thread(self.query_one("#model-line", Static).update, msg)
        if self.autostart and self.llm:
            self.call_from_thread(self.action_start)

    # ── actions ──────────────────────────────────────────────────────────
    @on(Button.Pressed, "#start")
    def _pressed(self) -> None:
        self.action_start()

    def action_start(self) -> None:
        if self.running or self.replay_dir:
            return
        repo = self.query_one("#repo", Input).value.strip()
        raw_issue = self.query_one("#issue", TextArea).text.strip()
        if not raw_issue:
            self.notify("Enter an issue first.", severity="error")
            return
        if not self.llm:
            self.notify("No working model yet. Check AI_API_KEY (see the model line).", severity="error")
            return
        try:
            issue = load_issue(raw_issue)
        except OSError as exc:
            self.notify(f"Could not read the issue: {exc}", severity="error")
            return
        repo = repo or issue.repo_url
        if not repo:
            self.notify("Enter a repository path or URL.", severity="error")
            return
        self._show_run()
        self.running = True
        self.started_at = time.time()
        self._run(repo, issue)

    def action_cancel(self) -> None:
        if self.running:
            self.cancel_event.set()
            self._log(Text("Cancelling after the current step...", style="yellow"))

    def action_diff(self) -> None:
        if self.last_patch:
            self._log(Syntax(self.last_patch, "diff", theme="ansi_dark", word_wrap=True))
        else:
            self.notify("No changes yet.")

    def _show_run(self) -> None:
        self.query_one("#form").display = False
        self.query_one("#run").display = True

    # ── background run ───────────────────────────────────────────────────
    @work(thread=True, exclusive=True, group="run")
    def _run(self, repo: str, issue) -> None:
        from .runner import execute
        try:
            result, report = execute(self.cfg, repo, issue, lambda e: self.call_from_thread(self._event, e),
                                     cancel=self.cancel_event, llm=self.llm)
            self.wrench_exit = 0 if result.status in ("verified", "no_change") else 3
        except Exception as exc:  # show it instead of crashing the UI
            self.call_from_thread(self._event, {"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
            self.wrench_exit = 1
        finally:
            self.running = False

    @work(thread=True, exclusive=True, group="run")
    def _replay(self, run_dir: Path) -> None:
        from .runner import replay
        self.started_at = time.time()
        self.call_from_thread(self._log, Text(f"Replaying {run_dir}", style="dim"))
        for event in replay(run_dir):
            self.call_from_thread(self._event, event)

    # ── rendering events ─────────────────────────────────────────────────
    def _log(self, renderable) -> None:
        self.query_one("#log", RichLog).write(renderable)

    def _set(self, wid: str, text) -> None:
        self.query_one(wid, Static).update(text)

    def _refresh_meter(self) -> None:
        if not self.started_at:
            return
        u = self.usage
        elapsed = int(time.time() - self.started_at) if self.running or self.replay_dir else int(u.get("_elapsed", 0))
        prompt, cached = u.get("prompt", 0), u.get("cached", 0)
        hit = f"{cached / prompt * 100:.0f}%" if prompt else "-"
        cost = u.get("cost")
        lines = [f"step        {self.step}  (attempt {self.attempt})",
                 f"requests    {u.get('requests', 0)}",
                 f"tokens in   {prompt:,}",
                 f"  cached    {cached:,}  ({hit})",
                 f"tokens out  {u.get('completion', 0):,}",
                 f"cost        {'$' + format(cost, '.4f') if cost is not None else 'n/a for this model'}",
                 f"time        {elapsed // 60}m {elapsed % 60:02d}s"]
        self._set("#meter", "\n".join(lines))

    def _event(self, e: Dict[str, Any]) -> None:
        k = e.get("kind")
        log = self._log
        if k == "start":
            self._set("#topbar", f"[b]{escape(e['issue'])}[/b]\n[dim]{escape(e['repo'])} · {escape(e['model'])} via "
                                 f"{escape(e['provider'])} · tests: {escape(str(e.get('test_command')))}[/dim]")
            for n in e.get("notes") or []:
                log(Text(f"note: {n}", style="dim"))
        elif k == "status":
            self.status_text = e["text"]
            self._set("#status", escape(e["text"]))
            log(Text(f"» {e['text']}", style="cyan"))
        elif k == "hints" and e.get("text"):
            lines = [l for l in e["text"].splitlines() if l.startswith("- ")][:6]
            log(Text("Likely files: " + ", ".join(l[2:].split("  ")[0] for l in lines), style="dim"))
        elif k == "attempt":
            self.attempt = e["number"]
            log(Text(f"── attempt {e['number']} ──", style="bold blue"))
        elif k == "step":
            self.step = e["number"]
            self._set("#status", f"step {e['number']} · {escape(self.status_text)}")
        elif k == "thinking":
            what = f"thinking {e.get('reasoning', 0):,} chars" if not e.get("content") else f"writing {e['content']:,} chars"
            self._set("#status", f"step {self.step} · model {what}")
        elif k == "assistant":
            if (e.get("text") or "").strip():
                log(Text(e["text"].strip()[:800], style="white"))
        elif k == "tool_call":
            t = Text()
            t.append(f"{_VERB.get(e['name'], e['name']):>6} ", style="bold magenta")
            t.append(_arg(e["name"], e.get("args") or {}))
            log(t)
            if e["name"] in ("edit_file", "write_file"):
                path = (e.get("args") or {}).get("path", "")
                if path and "SCRATCH" not in path:
                    self.files.add(path)
                    self._set("#files", escape("\n".join(sorted(self.files))))
        elif k == "tool_result":
            text = e.get("text", "")
            ok = e.get("ok", True)
            first = text.strip().splitlines()[0] if text.strip() else ""
            if not ok:
                log(Text(f"       {text[:400]}", style="red"))
            elif e["name"] in ("bash", "run_tests", "edit_file", "finish"):
                log(Text(f"       {first[:200]}", style="green" if e["name"] == "finish" else "dim"))
        elif k == "plan":
            self._set("#plan", escape(e["text"]))
        elif k == "warning":
            log(Text(f"! {e['text']}", style="yellow"))
        elif k == "error":
            log(Text(f"✗ {e['text']}", style="bold red"))
            self._set("#status", f"[red]{escape(e['text'][:200])}[/red]")
        elif k == "compact":
            log(Text(f"context: {e['text']}", style="dim"))
        elif k == "usage":
            self.usage.update({x: e.get(x) for x in ("prompt", "completion", "cached", "requests", "cost")})
            self._refresh_meter()
        elif k == "verify":
            lines = []
            if e.get("repro_command"):
                before = e.get("repro_before_exit")
                lines.append(f"repro on original: {'fails' if before not in (None, 0) else 'passes' if before == 0 else 'n/a'}")
                lines.append(f"repro with fix:    {'passes' if e.get('repro_after_exit') == 0 else 'FAILS'}")
                if e.get("bug_proven"):
                    lines.append("[green]bug proven fixed[/green]")
            if e.get("tests_ran"):
                lines.append(f"tests: {escape(e.get('tests_after_summary') or 'exit ' + str(e.get('tests_after_exit')))}")
                if e.get("new_failures"):
                    lines.append(f"[red]new failures: {escape(', '.join(e['new_failures'][:4]))}[/red]")
                elif e.get("preexisting_failures"):
                    lines.append(f"pre-existing failures: {len(e['preexisting_failures'])}")
            self._set("#evidence", "\n".join(lines) or "nothing to run")
            log(Text("  verify: " + "; ".join(Text.from_markup(l).plain for l in lines), style="cyan"))
        elif k == "review":
            if e.get("approved"):
                log(Text("  review: approved", style="green"))
            else:
                log(Text("  review: changes requested\n" + "\n".join("   - " + p for p in e.get("problems", [])),
                         style="yellow"))
        elif k == "done":
            status = e.get("status", "error")
            self.last_patch = e.get("patch") or ""
            self.usage["_elapsed"] = e.get("elapsed", 0)
            log(Text(f"\n{status.upper()}", style=_STATUS_STYLE.get(status, "bold")))
            if e.get("summary"):
                log(Text(e["summary"]))
            if self.last_patch:
                log(Syntax(self.last_patch[:12000], "diff", theme="ansi_dark", word_wrap=True))
            self._set("#status", f"[{_STATUS_STYLE.get(status, 'bold')}]{status.upper()}[/]")
        elif k == "report":
            log(Text(f"report: {e['path']}", style="bold"))
            self.notify(f"Report saved: {e['path']}", timeout=10)
