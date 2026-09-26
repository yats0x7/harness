"""Terminal UI: `make run`.

A launch screen in the style of modern coding CLIs (the horse, the name, the
model, a prompt), then a live view of the run: the agent's steps on the left;
the plan, a cost meter and the verification evidence on the right. The same
event stream drives headless mode and replay.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from . import __version__
from .config import Config
from .issue import load_issue
from .llm import (OLLAMA_URL, AuthError, Endpoint, LLMClient, LLMError, account_status, ensure_ollama_context,
                  local_models, resolve_endpoint)
from .logo import horse

_VERB = {"bash": "$", "read_file": "read", "search": "search", "find_files": "find", "list_dir": "ls",
         "edit_file": "edit", "write_file": "write", "undo_edit": "undo", "git_diff": "diff",
         "run_tests": "test", "update_plan": "plan", "finish": "finish"}
_STATUS_STYLE = {"verified": "bold #8fd18b", "no_change": "bold #f2c14e", "unverified": "bold #ef8a78",
                 "unfinished": "bold #ef8a78", "error": "bold #ef8a78"}

# Warm wood and bronze, taken from the logo. Text colours are chosen for at
# least 4.5:1 contrast on the background.
BG = "#15110d"
PANEL = "#1c1712"
RULE = "#3b3026"
TEXT = "#efe6da"
MUTED = "#a8997f"
GOLD = "#f2c14e"
WOOD = "#d59a55"
ERROR = "#ef8a78"

SHORTCUTS = (f"[{GOLD}]enter[/]  start the run      [{GOLD}]ctrl+j[/]  new line        [{GOLD}]tab[/]  switch field\n"
             f"[{GOLD}]ctrl+o[/] choose the model   [{GOLD}]ctrl+r[/]  replay last run  [{GOLD}]ctrl+q[/] quit")


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


def _short_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


class PromptArea(TextArea):
    """The issue prompt. Enter starts the run; ctrl+j adds a line; ? on an empty prompt shows shortcuts."""

    class Submitted(Message):
        pass

    class HelpToggled(Message):
        pass

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted())
            return
        if event.key == "ctrl+j":
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.character == "?" and not self.text:
            event.prevent_default()
            event.stop()
            self.post_message(self.HelpToggled())
            return
        await super()._on_key(event)


class ModelPicker(ModalScreen):
    """Choose between the provider's models and local Ollama models."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]
    DEFAULT_CSS = f"""
    ModelPicker {{ align: center middle; background: {BG} 70%; }}
    #picker {{ width: 76; height: auto; max-height: 30; background: {PANEL}; border: round {RULE}; padding: 1 2; }}
    #picker-title {{ color: {GOLD}; text-style: bold; margin-bottom: 1; }}
    #picker-note {{ color: {MUTED}; margin-top: 1; }}
    #models {{ height: auto; max-height: 20; background: {PANEL}; border: none; }}
    #models > .option-list--option-highlighted {{ background: {WOOD}; color: {BG}; text-style: bold; }}
    #models:focus > .option-list--option-highlighted {{ background: {GOLD}; color: {BG}; text-style: bold; }}
    #models > .option-list--option-disabled {{ color: {MUTED}; }}
    """

    def __init__(self, choices):
        super().__init__()
        self.choices = choices  # list of (label, value) where value is an Endpoint spec

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static("Choose the model", id="picker-title")
            yield OptionList(*[Option(label, id=str(i)) if value is not None else Option(label, disabled=True)
                               for i, (label, value) in enumerate(self.choices)], id="models")
            yield Static("enter to pick · esc to close. Local models run on this Mac through Ollama; a copy with a "
                         "32K context is made on first use.", id="picker-note")

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.choices[int(event.option.id)][1])


class TrojanApp(App):
    TITLE = "Trojan Horse"
    CSS = f"""
    Screen {{ background: {BG}; color: {TEXT}; }}

    #home {{ height: auto; padding: 1 2 0 2; }}
    #hero {{ height: auto; margin-bottom: 1; }}
    #logo {{ width: 26; height: auto; }}
    #info {{ height: auto; padding: 1 0 0 3; }}
    #brand {{ height: 1; }}
    #model-line, #repo-line {{ height: 1; color: {MUTED}; }}
    #tagline {{ height: auto; color: {MUTED}; margin-top: 1; }}

    #prompt-box {{ height: auto; border-top: solid {RULE}; border-bottom: solid {RULE}; padding: 0 0; }}
    .row {{ height: auto; }}
    .label {{ width: 7; color: {MUTED}; padding: 0 0 0 1; }}
    .caret {{ width: 7; color: {GOLD}; text-style: bold; padding: 0 0 0 1; }}
    #repo {{ width: 1fr; background: {BG}; border: none; padding: 0; color: {TEXT}; height: 1; }}
    #repo:focus {{ border: none; }}
    #issue {{ width: 1fr; height: auto; min-height: 1; max-height: 14; background: {BG}; border: none;
              padding: 0; color: {TEXT}; }}
    #issue:focus {{ border: none; }}
    #hints {{ height: 1; padding: 0 1; color: {MUTED}; }}
    #hint-left {{ width: 1fr; }}
    #hint-right {{ width: auto; }}
    #shortcuts {{ height: auto; padding: 1 1 0 1; color: {MUTED}; display: none; }}

    #run {{ height: 1fr; display: none; }}
    #topbar {{ height: 3; padding: 0 1; background: {PANEL}; border-bottom: solid {RULE}; }}
    #main {{ height: 1fr; }}
    #log {{ width: 3fr; border-right: solid {RULE}; padding: 0 1; background: {BG}; overflow-x: hidden; }}
    * {{ scrollbar-color: {RULE}; scrollbar-color-hover: {WOOD}; scrollbar-color-active: {GOLD};
         scrollbar-background: {BG}; scrollbar-background-hover: {BG}; scrollbar-background-active: {BG};
         scrollbar-corner-color: {BG}; scrollbar-size-vertical: 1; }}
    #side {{ width: 1fr; min-width: 38; padding: 0 1; background: {PANEL}; }}
    .panel-title {{ color: {GOLD}; text-style: bold; margin-top: 1; }}
    Footer {{ background: {PANEL}; }}
    """
    BINDINGS = [
        Binding("ctrl+r", "replay_last", "Replay last run", show=False),
        Binding("ctrl+o", "pick_model", "Choose model", show=False),
        Binding("d", "diff", "Show diff", show=True),
        Binding("c", "cancel", "Cancel run", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+s", "start", "Start", show=False),
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
        self.trojan_exit = 0
        self.credits: Optional[str] = None

    # ── layout ───────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Vertical(id="home"):
            with Horizontal(id="hero"):
                yield Static(horse(), id="logo")
                with Vertical(id="info"):
                    yield Static(Text.assemble(("Trojan Horse", f"bold {GOLD}"), ("  " + __version__, MUTED)),
                                 id="brand")
                    yield Static("connecting to the model...", id="model-line")
                    yield Static(self._repo_text(self.initial_repo), id="repo-line")
                    yield Static("Paste a GitHub issue link or describe a bug. It finds the code, fixes it,\n"
                                 "and proves the fix before it stops.", id="tagline")
            with Vertical(id="prompt-box"):
                with Horizontal(classes="row"):
                    yield Static("repo", classes="label")
                    yield Input(value=self.initial_repo, compact=True, id="repo",
                                placeholder="path or git URL (optional when the issue is a GitHub link)")
                with Horizontal(classes="row"):
                    yield Static(">", classes="caret")
                    yield PromptArea(self.initial_issue, id="issue", compact=True, soft_wrap=True,
                                     show_line_numbers=False, highlight_cursor_line=False,
                                     placeholder="GitHub issue URL, the issue text, or @path/to/issue.md")
            with Horizontal(id="hints"):
                yield Static("? for shortcuts", id="hint-left")
                yield Static("", id="hint-right")
            yield Static(SHORTCUTS, id="shortcuts")
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

    @staticmethod
    def _repo_text(repo: str) -> Text:
        if repo:
            return Text(_short_path(repo), style=MUTED)
        return Text(_short_path(os.getcwd()) + "  (set a repository below, or paste a GitHub issue link)", style=MUTED)

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh_meter)
        self.query_one(Footer).display = False
        if self.replay_dir:
            self._show_run()
            self._replay(self.replay_dir)
            return
        self.query_one("#issue", PromptArea).focus()
        self._connect()

    # ── model connection (runs off the UI thread) ────────────────────────
    @work(thread=True, exclusive=True, group="connect")
    def _connect(self) -> None:
        try:
            llm = LLMClient(resolve_endpoint(self.cfg), self.cfg.model)
        except (AuthError, LLMError) as exc:
            self.call_from_thread(self._set, "#model-line", Text(str(exc).splitlines()[0][:160] +
                                                                 "  (ctrl+o picks a local model)", style=ERROR))
            self.call_from_thread(self._set, "#hint-right", Text("no model · ctrl+o to choose", style=ERROR))
            return
        self._use(llm)
        if self.autostart and self.llm:
            self.call_from_thread(self.action_start)

    def _use(self, llm: LLMClient) -> None:
        """Switch to a model (called from worker threads) and show it with its credit status."""
        self.llm = llm
        self.credits = account_status(llm.endpoint)
        line = Text.assemble((llm.endpoint.model, f"bold {TEXT}"), (f"  via {llm.endpoint.provider}", MUTED),
                             (f"  ·  {self.credits}" if self.credits else "", MUTED))
        right = Text(f"{llm.endpoint.model} · ctrl+o to change", style=MUTED)
        self.call_from_thread(self._set, "#model-line", line)
        self.call_from_thread(self._set, "#hint-right", right)

    # ── actions ──────────────────────────────────────────────────────────
    @on(PromptArea.Submitted)
    def _submitted(self) -> None:
        self.action_start()

    @on(PromptArea.HelpToggled)
    def _help(self) -> None:
        panel = self.query_one("#shortcuts")
        panel.display = not panel.display

    @on(Input.Submitted, "#repo")
    def _repo_submitted(self) -> None:
        self.query_one("#issue", PromptArea).focus()

    @on(Input.Changed, "#repo")
    def _repo_changed(self, event: Input.Changed) -> None:
        self._set("#repo-line", self._repo_text(event.value.strip()))

    def action_start(self) -> None:
        if self.running or self.replay_dir:
            return
        repo = self.query_one("#repo", Input).value.strip()
        raw_issue = self.query_one("#issue", PromptArea).text.strip()
        if not raw_issue:
            self.notify("Describe the issue or paste a GitHub issue link first.", severity="warning")
            return
        if not self.llm:
            self.notify("No working model yet. Check AI_API_KEY; the reason is shown under the name.",
                        severity="error")
            return
        try:
            issue = load_issue(raw_issue)
        except OSError as exc:
            self.notify(f"Could not read the issue file: {exc}", severity="error")
            return
        repo = repo or issue.repo_url
        if not repo:
            self.notify("Set a repository (path or git URL), or paste a GitHub issue link.", severity="warning")
            self.query_one("#repo", Input).focus()
            return
        self._show_run()
        self.running = True
        self.started_at = time.time()
        self._run(repo, issue)

    def action_pick_model(self) -> None:
        if self.running:
            return
        self._gather_models()

    @work(thread=True, exclusive=True, group="models")
    def _gather_models(self) -> None:
        choices = []
        ep = self.llm.endpoint if self.llm else None
        remote = []
        if ep and ep.provider != "ollama":
            remote = [m for m in (ep.available or [ep.model])]
            fav = [m for m in remote if "deepseek" in m.lower() or "qwen" in m.lower()]
            remote = (fav or remote)[:25]
            if ep.model not in remote:
                remote.insert(0, ep.model)
        if remote:
            choices.append((f"── {ep.provider} (your AI_API_KEY) ──", None))
            for m in remote:
                mark = "  ● " if ep and m == ep.model else "    "
                choices.append((mark + m, ("remote", ep.provider, ep.base_url, m, ep.api_key, ep.max_output)))
        local = [m for m in local_models() if "-ctx" not in m]  # hide the auto-made large-context copies
        if local:
            choices.append(("── local, through Ollama ──", None))
            for m in local:
                mark = "  ● " if ep and ep.provider == "ollama" and m.startswith(ep.model) else "    "
                choices.append((mark + m, ("local", "ollama", OLLAMA_URL, m, "", 0)))
        if not choices:
            self.call_from_thread(self.notify, "No models found: set AI_API_KEY or start Ollama.", severity="warning")
            return
        self.call_from_thread(self.push_screen, ModelPicker(choices), self._model_chosen)

    def _model_chosen(self, value) -> None:
        if value:
            self._switch_model(value)

    @work(thread=True, exclusive=True, group="connect")
    def _switch_model(self, value) -> None:
        kind, provider, base_url, model, key, cap = value
        self.call_from_thread(self._set, "#model-line", Text(f"switching to {model}...", style=MUTED))
        if kind == "local":
            model = ensure_ollama_context(model)
        self._use(LLMClient(Endpoint(provider, base_url, model, key, [], cap), self.cfg.model))

    def action_replay_last(self) -> None:
        if self.running:
            return
        from .runner import latest_run
        run_dir = latest_run()
        if not run_dir:
            self.notify("No saved runs yet.", severity="warning")
            return
        self.replay_dir = run_dir
        self._show_run()
        self._replay(run_dir)

    def action_cancel(self) -> None:
        if self.running:
            self.cancel_event.set()
            self._log(Text("Cancelling after the current step...", style=GOLD))

    def action_diff(self) -> None:
        if self.last_patch:
            self._log(Syntax(self.last_patch, "diff", theme="ansi_dark", word_wrap=True))
        else:
            self.notify("No changes yet.")

    def _show_run(self) -> None:
        self.query_one("#home").display = False
        self.query_one("#run").display = True
        self.query_one(Footer).display = True

    # ── background run ───────────────────────────────────────────────────
    @work(thread=True, exclusive=True, group="run")
    def _run(self, repo: str, issue) -> None:
        from .runner import execute
        try:
            result, report = execute(self.cfg, repo, issue, lambda e: self.call_from_thread(self._event, e),
                                     cancel=self.cancel_event, llm=self.llm)
            self.trojan_exit = 0 if result.status in ("verified", "no_change") else 3
        except Exception as exc:  # show it instead of crashing the UI
            self.call_from_thread(self._event, {"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
            self.trojan_exit = 1
        finally:
            self.running = False

    @work(thread=True, exclusive=True, group="run")
    def _replay(self, run_dir: Path) -> None:
        from .runner import replay
        self.started_at = time.time()
        self.call_from_thread(self._log, Text(f"Replaying {run_dir}", style="dim"))
        for event in replay(run_dir):
            self.call_from_thread(self._event, event)

    @work(thread=True, group="credits")
    def _refresh_credits(self) -> None:
        if self.llm:
            self.credits = account_status(self.llm.endpoint) or self.credits
            self.call_from_thread(self._refresh_meter)

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
                 f"credits     {self.credits or 'n/a for this provider'}",
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
            log(Text(f"» {e['text']}", style=GOLD))
        elif k == "hints" and e.get("text"):
            lines = [l for l in e["text"].splitlines() if l.startswith("- ")][:6]
            log(Text("Likely files: " + ", ".join(l[2:].split("  ")[0] for l in lines), style="dim"))
        elif k == "attempt":
            self.attempt = e["number"]
            log(Text(f"── attempt {e['number']} ──", style=f"bold {GOLD}"))
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
            t.append(f"{_VERB.get(e['name'], e['name']):>6} ", style=f"bold {WOOD}")
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
                log(Text(f"       {text[:400]}", style=ERROR))
            elif e["name"] in ("bash", "run_tests", "edit_file", "finish"):
                log(Text(f"       {first[:200]}", style="#8fd18b" if e["name"] == "finish" else MUTED))
        elif k == "plan":
            self._set("#plan", escape(e["text"]))
        elif k == "warning":
            log(Text(f"! {e['text']}", style=GOLD))
        elif k == "error":
            log(Text(f"✗ {e['text']}", style=f"bold {ERROR}"))
            self._set("#status", f"[{ERROR}]{escape(e['text'][:200])}[/]")
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
                    lines.append("[#8fd18b]bug proven fixed[/]")
            if e.get("tests_ran"):
                lines.append(f"tests: {escape(e.get('tests_after_summary') or 'exit ' + str(e.get('tests_after_exit')))}")
                if e.get("new_failures"):
                    lines.append(f"[{ERROR}]new failures: {escape(', '.join(e['new_failures'][:4]))}[/]")
                elif e.get("preexisting_failures"):
                    lines.append(f"pre-existing failures: {len(e['preexisting_failures'])}")
            self._set("#evidence", "\n".join(lines) or "nothing to run")
            log(Text("  verify: " + "; ".join(Text.from_markup(l).plain for l in lines), style=GOLD))
        elif k == "review":
            if e.get("approved"):
                log(Text("  review: approved", style="#8fd18b"))
            else:
                log(Text("  review: changes requested\n" + "\n".join("   - " + p for p in e.get("problems", [])),
                         style=GOLD))
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
            self._refresh_credits()
            log(Text(f"report: {e['path']}", style="bold"))
            self.notify(f"Report saved: {e['path']}", timeout=10)
