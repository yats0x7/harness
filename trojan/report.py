"""The run report: what changed, and the evidence that it works."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .agent import RunResult
from .issue import Issue


def _baseline_status(verification: Dict[str, Any]) -> str:
    attempted = any(key in verification for key in ("repro_before_exit", "tests_before_exit"))
    repro_exit = verification.get("repro_before_exit")
    tests_exit = verification.get("tests_before_exit")
    repro_confirmed = (bool(verification.get("repro_independent"))
                       and not verification.get("repro_before_timed_out")
                       and (repro_exit is None or repro_exit not in (126, 127) and repro_exit >= 0))
    tests_ran = (bool(verification.get("tests_before_activity"))
                 and not verification.get("tests_before_timed_out")
                 and (tests_exit is None or tests_exit not in (126, 127) and tests_exit >= 0))
    if repro_confirmed or tests_ran:
        return "completed"
    if verification.get("repro_before_timed_out") or verification.get("tests_before_timed_out"):
        return "timed_out"
    if (repro_exit is not None and repro_exit < 0) or (tests_exit is not None and tests_exit < 0):
        return "incomplete"
    if verification.get("repro_before_exit") not in (None, 126, 127, -9):
        return "unconfirmed"
    return "failed_to_run" if attempted else "unavailable"


def _execution_status(result: RunResult) -> str:
    if result.error == "interrupted":
        return "interrupted"
    if result.error:
        return "failed"
    if any(attempt.reason == "cancelled" for attempt in result.attempts):
        return "cancelled"
    return "completed" if result.attempts else "not_started"


def _fmt_ver(v: Dict[str, Any]) -> str:
    rows = []
    if v.get("repro_command"):
        before = v.get("repro_before_exit")
        after = v.get("repro_after_exit")
        proof = "yes" if v.get("bug_proven") and v.get("repro_independent") else "no"
        rows.append(f"| Reproduction `{v['repro_command']}` | exit {before if before is not None else 'n/a'} "
                    f"| exit {after} | {proof} |")
    if v.get("tests_ran"):
        rows.append(f"| Tests `{v.get('tests_command')}` | {v.get('tests_before_summary') or ('exit ' + str(v['tests_before_exit']) if 'tests_before_exit' in v else 'not run')} "
                    f"| {v.get('tests_after_summary') or 'exit ' + str(v.get('tests_after_exit'))} | "
                    f"{'strong evidence' if v.get('tests_evidence') else 'not sufficient'} |")
    if not rows:
        return "No verification was possible.\n"
    out = "| Check | Original code | With the fix | Result |\n|---|---|---|---|\n" + "\n".join(rows) + "\n"
    if v.get("new_failures"):
        out += "\nNew failures: " + ", ".join(v["new_failures"]) + "\n"
    if v.get("preexisting_failures"):
        out += "\nFailing before and after (not caused by this change): " + ", ".join(v["preexisting_failures"][:20]) + "\n"
    if v.get("tests_note"):
        out += f"\n{v['tests_note']}\n"
    if v.get("tests_evidence_reason"):
        out += f"\nTest evidence: {v['tests_evidence_reason']}\n"
    if v.get("modified_existing_tests"):
        out += "\nModified existing tests (excluded as sole verification evidence): " + ", ".join(
            v["modified_existing_tests"]) + "\n"
    if v.get("decision_reason"):
        out += f"\nVerdict basis: {v['decision_reason']}\n"
    return out


def write_report(result: RunResult, issue: Issue) -> Path:
    d = result.run_dir
    best = result.best
    patch = best.patch if best else ""
    (d / "patch.diff").write_text(patch, encoding="utf-8")
    u = result.usage
    hit = (u.cached / u.prompt * 100) if u.prompt else 0.0
    summary = {
        "status": result.status, "task_type": issue.task.kind, "acceptance_criteria": list(issue.task.acceptance),
        "constraints": list(issue.task.constraints), "provider": result.provider, "model": result.model,
        "cost_usd": result.cost,
        "tournament": result.tournament,
        "elapsed_seconds": round(result.elapsed, 1), "requests": u.requests, "prompt_tokens": u.prompt,
        "completion_tokens": u.completion, "cached_prompt_tokens": u.cached, "cache_hit_percent": round(hit, 1),
        "attempts": [{"number": a.number, "status": a.status, "steps": a.steps, "reason": a.reason,
                      "verification": a.verification, "review": a.review, "tests_modified": a.tests_modified}
                     for a in result.attempts],
        "lifecycle": {
            "discovery": "completed",
            "execution": _execution_status(result),
            "baseline": _baseline_status(best.verification) if best else "unavailable",
            "verification": (best.verification.get("decision_reason", "insufficient evidence")
                             if best else "not_run"),
            "repair": ("performed" if len(result.attempts) > 1 else "not_needed"),
            "final_outcome": result.status,
        },
        "summary": best.summary if best else "", "error": result.error,
    }
    (d / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    lines = [f"# Trojan Horse run: {result.status.upper()}", "",
             f"**Task type:** {issue.task.kind}  ", f"**Request:** {issue.short}  ",
             f"**Model:** {result.model} via {result.provider}  ",
             f"**Time:** {result.elapsed:.0f}s · **Requests:** {u.requests} · **Tokens:** {u.prompt:,} in "
             f"({hit:.0f}% cached) / {u.completion:,} out", ""]
    if result.tournament:
        lines += [f"**Tournament:** {result.tournament.get('workers', 1)} independent workers; winner: "
                  f"worker {result.tournament.get('winner', '?')}", ""]
    if result.error:
        lines += [f"**Error:** {result.error}", ""]
    lifecycle = summary["lifecycle"]
    lines += ["## Run lifecycle", "",
              f"Discovery: {lifecycle['discovery']} · Execution: {lifecycle['execution']} · "
              f"Baseline: {lifecycle['baseline']} · Verification: {lifecycle['verification']} · "
              f"Repair: {lifecycle['repair']} · Outcome: {lifecycle['final_outcome']}", ""]
    if best:
        lines += ["## What changed", "", best.summary or "(no summary given)", "",
                  "## Evidence", "", _fmt_ver(best.verification)]
        if best.review:
            verdict = "approved" if best.review.get("approved") else "asked for changes"
            lines += [f"Reviewer {verdict}." + ("" if best.review.get("approved") else
                      " Points raised:\n" + "\n".join(f"- {p}" for p in best.review.get("problems", []))), ""]
        if best.tests_modified:
            lines += ["**Existing test files were modified:** " + ", ".join(best.tests_modified), ""]
        lines += ["## Patch", "", "```diff", patch.rstrip() or "(empty)", "```", ""]
    if len(result.attempts) > 1:
        lines += ["## Attempts", ""]
        for a in result.attempts:
            lines.append(f"- Attempt {a.number}: {a.status} after {a.steps} steps. {a.reason}")
        lines.append("")
    lines += ["Files: `patch.diff`, `summary.json`, `trajectory.jsonl` (every step), `outputs/` (full tool output)."]
    path = d / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
