"""The run report: what changed, and the evidence that it works."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .agent import RunResult
from .issue import Issue


def _fmt_ver(v: Dict[str, Any]) -> str:
    rows = []
    if v.get("repro_command"):
        before = v.get("repro_before_exit")
        after = v.get("repro_after_exit")
        proof = "yes" if v.get("bug_proven") else "no"
        rows.append(f"| Reproduction `{v['repro_command']}` | exit {before if before is not None else 'n/a'} "
                    f"| exit {after} | {proof} |")
    if v.get("tests_ran"):
        rows.append(f"| Tests `{v.get('tests_command')}` | {v.get('tests_before_summary') or ('exit ' + str(v['tests_before_exit']) if 'tests_before_exit' in v else 'not rerun (passed after)')} "
                    f"| {v.get('tests_after_summary') or 'exit ' + str(v.get('tests_after_exit'))} | "
                    f"{'no new failures' if not v.get('new_failures') and not v.get('suite_regressed') else 'NEW FAILURES'} |")
    if not rows:
        return "No verification was possible.\n"
    out = "| Check | Original code | With the fix | Result |\n|---|---|---|---|\n" + "\n".join(rows) + "\n"
    if v.get("new_failures"):
        out += "\nNew failures: " + ", ".join(v["new_failures"]) + "\n"
    if v.get("preexisting_failures"):
        out += "\nFailing before and after (not caused by this change): " + ", ".join(v["preexisting_failures"][:20]) + "\n"
    if v.get("tests_note"):
        out += f"\n{v['tests_note']}\n"
    return out


def write_report(result: RunResult, issue: Issue) -> Path:
    d = result.run_dir
    best = result.best
    patch = best.patch if best else ""
    (d / "patch.diff").write_text(patch, encoding="utf-8")
    u = result.usage
    hit = (u.cached / u.prompt * 100) if u.prompt else 0.0
    summary = {
        "status": result.status, "provider": result.provider, "model": result.model,
        "elapsed_seconds": round(result.elapsed, 1), "requests": u.requests, "prompt_tokens": u.prompt,
        "completion_tokens": u.completion, "cached_prompt_tokens": u.cached, "cache_hit_percent": round(hit, 1),
        "attempts": [{"number": a.number, "status": a.status, "steps": a.steps, "reason": a.reason,
                      "verification": a.verification, "review": a.review, "tests_modified": a.tests_modified}
                     for a in result.attempts],
        "summary": best.summary if best else "", "error": result.error,
    }
    (d / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    lines = [f"# Wrench run: {result.status.upper()}", "",
             f"**Issue:** {issue.short}  ", f"**Model:** {result.model} via {result.provider}  ",
             f"**Time:** {result.elapsed:.0f}s · **Requests:** {u.requests} · **Tokens:** {u.prompt:,} in "
             f"({hit:.0f}% cached) / {u.completion:,} out", ""]
    if result.error:
        lines += [f"**Error:** {result.error}", ""]
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
