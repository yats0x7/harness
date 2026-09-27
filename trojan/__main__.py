"""Command-line entry point: `python -m trojan`."""
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax

from . import __version__
from .config import load_config
from .issue import Issue, load_issue
from .llm import AuthError, LLMError, resolve_endpoint, LLMClient

console = Console(highlight=False)

_ICONS = {"bash": "$", "read_file": "read", "search": "search", "find_files": "find", "list_dir": "ls",
          "edit_file": "edit", "write_file": "write", "undo_edit": "undo", "git_diff": "diff",
          "run_tests": "tests", "update_plan": "plan", "finish": "finish"}


def _arg_line(name: str, args: dict) -> str:
    if name == "bash":
        return args.get("command", "")
    if name in ("edit_file", "write_file", "read_file", "undo_edit"):
        extra = ""
        if name == "read_file" and args.get("start_line"):
            extra = f" :{args.get('start_line')}-{args.get('end_line', '')}"
        return str(args.get("path", "")) + extra
    if name == "search":
        return f"{args.get('pattern', '')!r}" + (f" in {args['path']}" if args.get("path") else "")
    if name == "finish":
        return str(args.get("summary", ""))[:200]
    return " ".join(f"{k}={v}" for k, v in args.items())[:200]


def print_event(e: dict) -> None:
    k = e.get("kind")
    if k == "start":
        console.print(Panel.fit(f"[b]{escape(e['issue'])}[/b]\nrepo: {escape(e['repo'])}\nmodel: {e['model']} "
                                f"via {e['provider']}\ntests: {escape(str(e.get('test_command')))}",
                                title="Trojan Horse", border_style="#c98a45"))
        for n in e.get("notes") or []:
            console.print(f"[dim]note: {escape(n)}[/dim]")
    elif k == "status":
        console.print(f"[cyan]»[/cyan] {escape(e['text'])}")
    elif k == "hints":
        if e.get("text"):
            console.print(f"[dim]{escape(e['text'].splitlines()[0])}[/dim]")
            for line in e["text"].splitlines()[1:9]:
                if line.startswith("- "):
                    console.print(f"[dim]  {escape(line)}[/dim]")
    elif k == "attempt":
        console.rule(f"attempt {e['number']}")
    elif k == "step":
        pass
    elif k == "assistant":
        text = (e.get("text") or "").strip()
        if text:
            console.print(f"[white]{escape(text[:600])}[/white]")
    elif k == "tool_call":
        name = e["name"]
        console.print(f"[bold magenta]{_ICONS.get(name, name):>6}[/bold magenta] {escape(_arg_line(name, e.get('args', {})))}")
    elif k == "tool_result":
        text = e.get("text", "")
        first = text.strip().splitlines()[0] if text.strip() else ""
        style = "green" if e.get("ok") else "red"
        if e["name"] in ("finish",) or not e.get("ok"):
            console.print(f"[{style}]       {escape(text[:500])}[/{style}]")
        elif e["name"] in ("bash", "run_tests"):
            console.print(f"[dim]       {escape(first[:160])}[/dim]")
    elif k == "plan":
        console.print(Panel(escape(e["text"]), title="plan", border_style="dim", expand=False))
    elif k == "warning":
        console.print(f"[yellow]! {escape(e['text'])}[/yellow]")
    elif k == "error":
        console.print(f"[red]✗ {escape(e['text'])}[/red]")
    elif k == "compact":
        console.print(f"[dim]context: {escape(e['text'])}[/dim]")
    elif k == "verify":
        parts = []
        if e.get("repro_command"):
            parts.append(f"repro before: exit {e.get('repro_before_exit', 'n/a')}, after: exit {e.get('repro_after_exit')}")
        if e.get("tests_ran"):
            parts.append(f"tests: {e.get('tests_after_summary') or 'exit ' + str(e.get('tests_after_exit'))}")
        if e.get("new_failures"):
            parts.append(f"new failures: {', '.join(e['new_failures'][:5])}")
        console.print(f"[cyan]  verify[/cyan] " + escape("; ".join(parts) or "nothing to run"))
    elif k == "review":
        if e.get("approved"):
            console.print("[green]  review: approved[/green]")
        else:
            console.print("[yellow]  review: changes requested[/yellow]\n" + escape("\n".join("   - " + p for p in e.get("problems", []))))
    elif k == "usage":
        pass
    elif k == "done":
        status = e["status"]
        color = {"verified": "green", "no_change": "yellow"}.get(status, "red")
        console.rule(f"[{color}]{status.upper()}[/{color}]")
        if e.get("summary"):
            console.print(escape(e["summary"]))
        if e.get("patch"):
            console.print(Syntax(e["patch"][:8000], "diff", theme="ansi_dark", word_wrap=True))
    elif k == "report":
        console.print(f"report: {e['path']}")


def console_approver():
    """Ask on the terminal before each command or edit: y = yes, n = no (with a note), a = yes to all."""
    state = {"all": False}

    def approve(name, args, preview):
        if state["all"]:
            return True, ""
        console.print(Panel(Syntax(preview, "diff" if name == "edit_file" else "bash", theme="ansi_dark",
                                   word_wrap=True), title=f"approve {name}?", border_style="#f2c14e"))
        while True:
            answer = console.input("[b]y[/b]es / [b]n[/b]o / [b]a[/b]lways: ").strip().lower()
            if answer in ("y", "yes", ""):
                return True, ""
            if answer in ("a", "always"):
                state["all"] = True
                return True, ""
            if answer in ("n", "no"):
                return False, console.input("note for the agent (optional): ").strip()

    return approve


def doctor(cfg) -> int:
    console.print(f"Trojan Horse {__version__} · config {cfg.path}")
    key = cfg.api_key
    console.print(f"AI_API_KEY: {'set (' + str(len(key)) + ' chars)' if key else '[red]not set[/red]'}")
    try:
        ep = resolve_endpoint(cfg)
    except (AuthError, LLMError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 1
    console.print(f"provider: {ep.provider}  base_url: {ep.base_url}\nmodel: [b]{ep.model}[/b]"
                  + (f"  ({len(ep.available)} models listed)" if ep.available else ""))
    llm = LLMClient(ep, cfg.model)
    tool = {"type": "function", "function": {"name": "add", "description": "Add two integers.",
            "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                           "required": ["a", "b"]}}}
    try:
        reply = llm.chat([{"role": "user", "content": "Use the add tool to add 2 and 3."}], tools=[tool], max_tokens=2048)
    except LLMError as exc:
        console.print(f"[red]chat call failed: {escape(str(exc))}[/red]")
        return 1
    if reply.tool_calls:
        console.print(f"[green]native tool calling works[/green]: {reply.tool_calls[0].name}({reply.tool_calls[0].arguments})")
    else:
        console.print(f"[yellow]no native tool call returned; text fallback will be used if needed[/yellow]: {escape(reply.content[:200])}")
    console.print(f"tokens: {reply.usage.prompt} in / {reply.usage.completion} out")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="trojan", description="Trojan Horse: an autonomous coding-agent harness.")
    p.add_argument("--repo", help="path or git URL of the repository to fix")
    p.add_argument("--issue", "--task", dest="issue",
                   help="task text, @file, a file path, a GitHub issue URL, or - for stdin")
    p.add_argument("--headless", action="store_true", help="plain console output instead of the TUI")
    p.add_argument("--check", action="store_true", help="check the key, provider and model, then exit")
    p.add_argument("--discover", action="store_true", help="read-only repository discovery; no model or edits")
    p.add_argument("--lenses", default="error,test,structural", help="discovery lenses: error,test,structural")
    p.add_argument("--replay", nargs="?", const="latest", help="replay a saved run (default: the latest)")
    p.add_argument("--max-steps", type=int)
    p.add_argument("--attempts", type=int)
    p.add_argument("--best-of", type=int, default=1, metavar="N",
                   help="run up to N independent workers and keep the strongest result (1-4; default: 1)")
    p.add_argument("--no-review", action="store_true")
    p.add_argument("--publish", action="store_true",
                   help="after a verified run, push a branch and open a GitHub pull request")
    p.add_argument("--fork", action="store_true", help="publish through a GitHub fork (requires --publish)")
    p.add_argument("--branch", help="safe branch name for --publish; defaults to trojan/<task>-<timestamp>")
    p.add_argument("--pr-title", help="pull request title for --publish")
    p.add_argument("--approval", choices=["auto", "ask"], help="ask before shell commands and file edits")
    p.add_argument("--skills", action="store_true", help="list the installed skills and exit")
    p.add_argument("--new-skill", metavar="NAME", help="create a skill template in ~/.trojan/skills")
    p.add_argument("--config")
    p.add_argument("--version", action="version", version=f"Trojan Horse {__version__}")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    if args.max_steps:
        cfg.agent.max_steps = args.max_steps
    if args.attempts:
        cfg.agent.max_attempts = args.attempts
    if args.no_review:
        cfg.agent.review = False

    if args.best_of < 1 or args.best_of > 4:
        p.error("--best-of must be between 1 and 4")

    if args.fork and not args.publish:
        p.error("--fork requires --publish")

    if args.discover:
        from .discovery import discover_repository, render
        try:
            print(render(discover_repository(args.repo or os.getcwd(), args.lenses.split(","))))
        except (OSError, ValueError) as exc:
            console.print(f"[red]Discovery failed: {escape(str(exc))}[/red]")
            return 2
        return 0

    if args.approval:
        cfg.agent.approval = args.approval

    if args.check:
        return doctor(cfg)

    if args.skills or args.new_skill:
        from .skills import create, discover, skill_dirs
        if args.new_skill:
            try:
                path = create(args.new_skill)
            except ValueError as exc:
                console.print(f"[red]{escape(str(exc))}[/red]")
                return 2
            console.print(f"Created {path}\nEdit its description and instructions; the agent picks it up on the next run.")
            return 0
        found = discover(Path.cwd())
        for s in sorted(found.values(), key=lambda s: s.name):
            console.print(f"[b]{s.name}[/b] [dim]({s.source}: {s.path})[/dim]\n  {escape(s.description)}")
        console.print(f"[dim]{len(found)} skills. Searched: " + ", ".join(str(d) for d, _ in skill_dirs(Path.cwd())) + "[/dim]")
        return 0

    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.headless and not args.publish

    if args.replay:
        from .runner import latest_run, replay
        run_dir = latest_run() if args.replay == "latest" else Path(args.replay)
        if not run_dir:
            console.print("[red]no saved runs to replay[/red]")
            return 1
        if interactive:
            from .tui import TrojanApp
            TrojanApp(cfg, replay_dir=run_dir).run()
            return 0
        for event in replay(run_dir):
            print_event(event)
        return 0

    from .issue import IssueFetchError
    issue = None
    try:
        if args.issue:
            issue = load_issue(args.issue)
        elif not sys.stdin.isatty():
            text = sys.stdin.read()
            if text.strip():
                issue = load_issue(text)
    except IssueFetchError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 2
    repo = args.repo or (issue.repo_url if issue else "") or ""

    if interactive:
        from .tui import TrojanApp
        app = TrojanApp(cfg, repo=repo, issue_text=args.issue or "", autostart=bool(repo and issue))
        app.run()
        return app.trojan_exit

    if not issue:
        console.print("[red]No task given. Use --task/--issue (text, @file, a GitHub URL) or pipe it on stdin.[/red]")
        return 2
    if not repo:
        repo = os.getcwd()
        console.print(f"[yellow]No --repo given; using the current directory {repo}[/yellow]")

    import signal

    def _terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)  # an external timeout still gets a report
    approver = None
    if cfg.agent.approval == "ask":
        if sys.stdin.isatty():
            approver = console_approver()
        else:
            console.print("[yellow]--approval ask needs an interactive terminal; running with auto-approve.[/yellow]")
    from .runner import execute
    try:
        result, report = execute(cfg, repo, issue, print_event, approver=approver, best_of=args.best_of)
        if args.publish:
            if result.status != "verified":
                raise RuntimeError(f"publishing requires a verified run, got {result.status}")
            from .github import GitHubPublishError, _repo_from_local, publish_verified_patch
            repo_url = issue.repo_url or (repo if repo.startswith("https://github.com/") else _repo_from_local(repo))
            if not repo_url:
                raise GitHubPublishError("publishing requires a GitHub repository URL or a local repo with an origin")
            pr = publish_verified_patch(report.parent / "patch.diff", repo_url, task_kind=issue.kind,
                                        title=args.pr_title or issue.short, body=issue.text,
                                        branch=args.branch, fork=args.fork)
            console.print(f"[green]Pull request opened:[/green] {pr.url}")
    except (AuthError, LLMError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 1
    except RuntimeError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return 1
    return 0 if result.status in ("verified", "no_change") else 3


if __name__ == "__main__":
    sys.exit(main())
