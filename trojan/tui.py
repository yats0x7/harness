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
from .spinner import VERB_SECONDS, frame, next_verb

_VERB = {"bash": "$", "read_file": "read", "search": "search", "find_files": "find", "list_dir": "ls",
         "edit_file": "edit", "write_file": "write", "undo_edit": "undo", "git_diff": "diff",
         "run_tests": "test", "update_plan": "plan", "finish": "finish", "use_skill": "skill"}
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

SHORTCUTS = (f"[{GOLD}]enter[/]  start the run      [{GOLD}]ctrl+j[/]  new line          [{GOLD}]tab[/]    switch field\n"
             f"[{GOLD}]ctrl+o[/] choose the model   [{GOLD}]ctrl+t[/]  ask / auto-approve  [{GOLD}]ctrl+r[/] replay last run\n"
             f"[{GOLD}]ctrl+n[/] new task (after a run)  [{GOLD}]ctrl+q[/] quit     skills: add a folder with SKILL.md to ~/.trojan/skills (make skill NAME=x)")
MODE_LABEL = {"auto": "auto-approve", "ask": "ask before changes"}


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
    if name == "use_skill":
        return args.get("name", "")
    if name == "finish":
        return args.get("summary", "")[:160]
    return " ".join(f"{k}={v}" for k, v in args.items())[:160]


def _short_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


class PromptArea(TextArea):
    """The universal task prompt. Enter starts the run; ctrl+j adds a line; ? shows shortcuts."""

    class Submitted(Message):
        def __init__(self, area: "PromptArea") -> None:
            self.area = area
            super().__init__()

    class HelpToggled(Message):
        pass

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self))
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


class ApprovalScreen(ModalScreen):
    """Ask the user to approve one command or file change."""

    BINDINGS = [Binding("y", "answer('yes')", "Approve"), Binding("a", "answer('all')", "Approve all"),
                Binding("n", "answer('no')", "Reject"), Binding("escape", "answer('no')", "Reject")]
    DEFAULT_CSS = f"""
    ApprovalScreen {{ align: center bottom; background: {BG} 25%; }}
    #approval {{ width: 100; height: auto; max-height: 40; margin-bottom: 2; background: {PANEL}; border: round {GOLD}; padding: 1 2; }}
    #approval-title {{ color: {GOLD}; text-style: bold; margin-bottom: 1; }}
    #approval-preview {{ height: auto; max-height: 26; background: {BG}; padding: 0 1; }}
    #approval-keys {{ color: {MUTED}; margin-top: 1; }}
    #approval-note {{ display: none; margin-top: 1; background: {BG}; border: none; height: 1; }}
    """

    def __init__(self, name: str, preview: str):
        super().__init__()
        self.tool = name
        self.preview = preview

    def compose(self) -> ComposeResult:
        verb = {"bash": "Run this command?", "edit_file": "Apply this edit?", "write_file": "Write this file?",
                "undo_edit": "Undo the last edit to this file?"}.get(self.tool, f"Allow {self.tool}?")
        with Vertical(id="approval"):
            yield Static(verb, id="approval-title")
            with VerticalScroll(id="approval-preview"):
                yield Static(Syntax(self.preview, "diff" if self.tool == "edit_file" else "bash",
                                    theme="ansi_dark", word_wrap=True))
            yield Static(f"[{GOLD}]y[/] approve   [{GOLD}]a[/] approve all from now on   "
                         f"[{GOLD}]n[/] reject (you can add a note for the agent)", id="approval-keys")
            yield Input(placeholder="note for the agent, then enter (optional)", id="approval-note", compact=True)

    def action_answer(self, answer: str) -> None:
        if answer == "no":
            note = self.query_one("#approval-note", Input)
            if not note.display:
                note.display = True
                note.focus()
                return
            self.dismiss((False, note.value.strip(), False))
        else:
            self.dismiss((True, "", answer == "all"))

    @on(Input.Submitted, "#approval-note")
    def _note(self, event: Input.Submitted) -> None:
        self.dismiss((False, event.value.strip(), False))


class ModelPicker(ModalScreen):
    """Choose between the provider's models and local Ollama models."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]
    DEFAULT_CSS = f"""
    ModelPicker {{ align: center middle; background: {BG} 40%; }}
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
    #model-line, #repo-line, #skills-line {{ height: 1; color: {MUTED}; }}
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
    #activity {{ height: 1; padding: 0 1; background: {BG}; color: {MUTED}; }}
    #followup {{ height: auto; max-height: 8; border-top: solid {GOLD}; background: {BG}; display: none; }}
    #followup-input {{ width: 1fr; height: auto; min-height: 1; max-height: 6; background: {BG}; border: none;
                       padding: 0; color: {TEXT}; }}
    #followup-input:focus {{ border: none; }}
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
        Binding("ctrl+t", "toggle_approval", "Ask / auto-approve", show=True),
        Binding("ctrl+n", "new_task", "New task", show=True),
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
        # the live activity line: "thinking" (spinner + rotating verb), "working" (spinner + label) or idle
        self.activity = "idle"
        self.activity_label = ""
        self.activity_since = 0.0
        self.verb = next_verb()
        self.verb_since = 0.0
        self.tick = 0
        self.think_chars = 0
        self.last_repo = repo
        self.last_title = ""
        self.last_summary = ""
        self.approval_mode = cfg.agent.approval if cfg.agent.approval in MODE_LABEL else "auto"

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
                    yield Static(self._skills_text(), id="skills-line")
                    yield Static("Describe a bug, feature, refactor, test, documentation task, or review.\n"
                                 "It finds the code, works the task, and proves the result before it stops.", id="tagline")
            with Vertical(id="prompt-box"):
                with Horizontal(classes="row"):
                    yield Static("repo", classes="label")
                    yield Input(value=self.initial_repo, compact=True, id="repo",
                                placeholder="path or git URL (optional; defaults to the current directory)")
                with Horizontal(classes="row"):
                    yield Static(">", classes="caret")
                    yield PromptArea(self.initial_issue, id="issue", compact=True, soft_wrap=True,
                                     show_line_numbers=False, highlight_cursor_line=False,
                                     placeholder="Task text, GitHub issue URL, or @path/to/task.md")
            with Horizontal(id="hints"):
                yield Static("? for shortcuts", id="hint-left")
                yield Static("", id="hint-right")
            yield Static(SHORTCUTS, id="shortcuts")
        with Vertical(id="run"):
            yield Static("", id="topbar")
            yield Static("", id="activity")
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
            with Horizontal(id="followup"):
                yield Static(">", classes="caret")
                yield PromptArea("", id="followup-input", compact=True, soft_wrap=True, show_line_numbers=False,
                                 highlight_cursor_line=False,
                                 placeholder="Next task for this repo: a follow-up (builds on these changes) or a "
                                             "GitHub issue link · enter to run · ctrl+n for a new repo")
        yield Footer()

    @staticmethod
    def _skills_text() -> Text:
        from .skills import discover
        n = len(discover())
        return Text(f"{n} skill{'s' if n != 1 else ''} ready  ·  add your own in ~/.trojan/skills", style=MUTED)

    @staticmethod
    def _repo_text(repo: str) -> Text:
        if repo:
            return Text(_short_path(repo), style=MUTED)
        return Text(_short_path(os.getcwd()) + "  (default repository; paste a GitHub issue link to override)", style=MUTED)

    def _refresh_hint(self) -> None:
        model = self.llm.endpoint.model if self.llm else "no model"
        mode_style = GOLD if self.approval_mode == "ask" else MUTED
        self._set("#hint-right", Text.assemble((f"{model} (ctrl+o)  ·  ", MUTED),
                                               (MODE_LABEL[self.approval_mode], mode_style), (" (ctrl+t)", MUTED)))

    def action_toggle_approval(self) -> None:
        self.approval_mode = "ask" if self.approval_mode == "auto" else "auto"
        self._refresh_hint()
        self.notify(f"Mode: {MODE_LABEL[self.approval_mode]}"
                    + (". You will approve each command and file edit." if self.approval_mode == "ask" else "."))

    def _approve(self, name: str, args: Dict[str, Any], preview: str):
        """Called from the agent's thread before a command or edit; blocks until the user answers."""
        if self.approval_mode == "auto":
            return True, ""
        answered = threading.Event()
        box: Dict[str, Any] = {}

        def done(result) -> None:
            box["result"] = result
            answered.set()

        screen = ApprovalScreen(name, preview)
        self.call_from_thread(self.push_screen, screen, done)
        while not answered.wait(0.5):
            if self.cancel_event.is_set():
                def dismiss_cancelled() -> None:
                    if self.screen is screen:
                        screen.dismiss((False, "the run was cancelled", False))
                    elif not answered.is_set():
                        done((False, "the run was cancelled", False))

                self.call_from_thread(dismiss_cancelled)
                answered.wait(1)
                break
        approved, note, always = box.get("result") or (False, "", False)
        if always:
            self.approval_mode = "auto"
            self.call_from_thread(self._refresh_hint)
        return approved, note

    def _set_activity(self, mode: str, label: str = "") -> None:
        now = time.time()
        if mode == "thinking" and self.activity != "thinking":
            self.verb, self.verb_since, self.think_chars = next_verb(self.verb), now, 0
        if mode != self.activity or label != self.activity_label:
            self.activity_since = now
        self.activity, self.activity_label = mode, label
        self._spin_activity()

    def _spin_activity(self) -> None:
        if not self._main_screen().query("#activity"):
            return
        if self.activity == "idle":
            self._set("#activity", "")
            return
        self.tick += 1
        now = time.time()
        elapsed = int(now - self.activity_since)
        spin = Text(frame(self.tick) + " ", style=f"bold {GOLD}")
        if self.activity == "thinking":
            if now - self.verb_since >= VERB_SECONDS:
                self.verb, self.verb_since = next_verb(self.verb), now
            detail = f"  {elapsed}s" + (f" · {self.think_chars:,} chars" if self.think_chars else "")
            line = spin + Text(self.verb + "…", style=f"bold {GOLD}") + Text(detail, style=MUTED)
        else:
            line = spin + Text(self.activity_label, style=TEXT) + Text(f"  {elapsed}s", style=MUTED)
        self._set("#activity", line)

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh_meter)
        self.set_interval(0.1, self._spin_activity)
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
        self.call_from_thread(self._set, "#model-line", line)
        self.call_from_thread(self._refresh_hint)

    # ── actions ──────────────────────────────────────────────────────────
    @on(PromptArea.Submitted)
    def _submitted(self, event: PromptArea.Submitted) -> None:
        if event.area.id == "followup-input":
            self._start_followup(event.area.text.strip())
        else:
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
            self.notify("Describe the task or paste a GitHub issue link first.", severity="warning")
            return
        if not self.llm:
            self.notify("No working model yet. Check AI_API_KEY; the reason is shown under the name.",
                        severity="error")
            return
        from .issue import IssueFetchError
        try:
            issue = load_issue(raw_issue)
        except IssueFetchError as exc:
            self.notify(str(exc), severity="error", timeout=12)
            return
        except OSError as exc:
            self.notify(f"Could not read the issue file: {exc}", severity="error")
            return
        repo = repo or issue.repo_url or os.getcwd()
        self._reset_run_view()
        self._show_run()
        self.last_repo = repo
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
                                     cancel=self.cancel_event, llm=self.llm, approver=self._approve)
            self.trojan_exit = 0 if result.status in ("verified", "no_change") else 3
        except Exception as exc:  # show it instead of crashing the UI
            self.call_from_thread(self._event, {"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
            self.trojan_exit = 1
        finally:
            self.running = False
            self.call_from_thread(self._open_followup)

    def _open_followup(self) -> None:
        bar = self.query_one("#followup")
        bar.display = True
        box = self.query_one("#followup-input", PromptArea)
        box.load_text("")
        box.focus()
        self._log(Text("Type a follow-up below to keep going on this repo, or press ctrl+n for a new task.",
                       style=MUTED))

    def _reset_run_view(self) -> None:
        self.usage, self.step, self.attempt, self.files, self.last_patch = {}, 0, 1, set(), ""
        self.cancel_event = threading.Event()
        for wid, text in (("#plan", "(waiting for the agent)"), ("#files", "none yet"),
                          ("#evidence", "not verified yet"), ("#status", "")):
            self._set(wid, text)

    def _start_followup(self, text: str) -> None:
        if self.running or not text:
            return
        from .issue import Issue, IssueFetchError
        if text.startswith(("http://", "https://", "@")):
            try:
                issue = load_issue(text)  # a separate, self-contained task
            except (IssueFetchError, OSError) as exc:
                self.notify(str(exc), severity="error", timeout=12)
                return
        else:
            previous = self.last_title or "the previous task"
            done = self.last_summary or "(no summary was recorded)"
            issue = Issue(title=text.splitlines()[0][:100], text=(
                f"Follow-up request in the same repository.\n\n"
                f"Previous task: {previous}\n"
                f"What was done for it (these changes are already in the working tree, keep them): {done}\n\n"
                f"New request:\n{text}"))
        if not self.llm:
            self.notify("No working model. Press ctrl+o to choose one.", severity="error")
            return
        self.query_one("#followup").display = False
        self._reset_run_view()
        self._log(Text("\n" + "━" * 60, style=RULE))
        self._log(Text(f"next task: {issue.short}", style=f"bold {GOLD}"))
        self.running = True
        self.started_at = time.time()
        self._run(self.last_repo, issue)

    def action_new_task(self) -> None:
        if self.running:
            self.notify("A run is in progress. Press c to cancel it first.", severity="warning")
            return
        self.replay_dir = None
        self.query_one("#run").display = False
        self.query_one(Footer).display = False
        self.query_one("#followup").display = False
        self.query_one("#home").display = True
        self.query_one("#repo", Input).value = self.last_repo or ""
        box = self.query_one("#issue", PromptArea)
        box.load_text("")
        box.focus()

    @work(thread=True, exclusive=True, group="run")
    def _replay(self, run_dir: Path) -> None:
        from .runner import replay
        self.started_at = time.time()
        self.call_from_thread(self._log, Text(f"Replaying {run_dir}", style="dim"))
        for event in replay(run_dir):
            self.call_from_thread(self._event, event)
        self.call_from_thread(self._log, Text("Replay finished. Press ctrl+n to start a task.", style=MUTED))

    @work(thread=True, group="credits")
    def _refresh_credits(self) -> None:
        if self.llm:
            self.credits = account_status(self.llm.endpoint) or self.credits
            self.call_from_thread(self._refresh_meter)

    # ── rendering events ─────────────────────────────────────────────────
    def _main_screen(self):
        """Return the base screen even while a modal approval screen is open."""
        stack = self.screen_stack
        return stack[0] if stack else self.screen

    def _log(self, renderable) -> None:
        self._main_screen().query_one("#log", RichLog).write(renderable)

    def _set(self, wid: str, text) -> None:
        self._main_screen().query_one(wid, Static).update(text)

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
            self.last_repo = e.get("repo") or self.last_repo
            self.last_title = e.get("issue") or ""
            self._set("#topbar", f"[b]{escape(e['issue'])}[/b]\n[dim]{escape(e['repo'])} · {escape(e['model'])} via "
                                 f"{escape(e['provider'])} · tests: {escape(str(e.get('test_command')))}[/dim]")
            for n in e.get("notes") or []:
                log(Text(f"note: {n}", style="dim"))
        elif k == "status":
            self._set_activity("working", e["text"])
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
            self._set_activity("thinking")
            self.status_text = "model is thinking"
            self.step = e["number"]
            self._set("#status", f"step {e['number']} · {escape(self.status_text)}")
        elif k == "thinking":
            self.think_chars = int(e.get("reasoning", 0)) + int(e.get("content", 0))
            what = f"thinking {e.get('reasoning', 0):,} chars" if not e.get("content") else f"writing {e['content']:,} chars"
            self._set("#status", f"step {self.step} · model {what}")
        elif k == "assistant":
            if (e.get("text") or "").strip():
                log(Text(e["text"].strip()[:800], style="white"))
        elif k == "tool_call":
            label = {"bash": "running a command", "run_tests": "running the tests", "read_file": "reading",
                     "search": "searching", "edit_file": "editing", "write_file": "writing",
                     "finish": "wrapping up", "use_skill": "loading a skill"}.get(e["name"], e["name"])
            target = _arg(e["name"], e.get("args") or {})
            self._set_activity("working", f"{label} {target[:70]}".strip())
            self.status_text = label
            self._set("#status", f"step {self.step} · {escape(label)}")
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
            self._set_activity("idle")
            self.last_summary = e.get("summary") or ""
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
