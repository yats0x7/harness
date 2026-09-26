"""Cheap fault localisation before the first model call.

Pulls identifiers, file paths and traceback frames out of the issue, greps for
them, and ranks files so that rare, specific terms count for more than common
ones. The model gets the ranking as hints plus a short outline of the top
files, which saves it several exploration turns.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

from .workspace import SKIP_DIRS, Workspace

_STOP = {
    "the", "and", "for", "with", "this", "that", "from", "when", "then", "should", "would", "could", "error",
    "issue", "bug", "true", "false", "none", "null", "self", "return", "print", "import", "value", "values",
    "data", "list", "dict", "string", "number", "file", "files", "test", "tests", "expected", "actual", "result",
    "output", "input", "example", "python", "version", "using", "used", "also", "into", "have", "does", "not",
    "but", "are", "was", "were", "been", "what", "which", "there", "their", "some", "like", "just", "only",
    "function", "method", "class", "object", "type", "name", "line", "code", "call", "works", "work", "get", "set",
}
_CODE_EXT = r"(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|go|rs|java|kt|rb|php|c|cc|cpp|h|hpp|cs|swift|scala|m|sh|toml|yaml|yml|json|cfg|ini)"
_PATH = re.compile(rf"[\w./-]*\w\.{_CODE_EXT}\b")
_TRACE = re.compile(r'File "([^"]+)", line (\d+)')
_JS_TRACE = re.compile(r"at .*?\(?([\w./-]+\.(?:js|ts|mjs|cjs|jsx|tsx)):(\d+)")
_BACKTICK = re.compile(r"`([^`\n]{2,80})`")
_IDENT = re.compile(r"\b(?:[A-Za-z_][A-Za-z0-9_]*[._])+[A-Za-z0-9_]+\b|\b[a-z]+[A-Z][A-Za-z0-9]*\b|\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b|\b[a-z0-9]+_[a-z0-9_]+\b")
_ERROR = re.compile(r"\b[A-Z]\w*(?:Error|Exception|Warning)\b")


def extract_terms(issue: str) -> Tuple[List[str], List[str]]:
    """Return (identifier-like terms, path-like strings) mentioned in the issue."""
    paths = list(dict.fromkeys(m.group(0).lstrip("./") for m in _PATH.finditer(issue)))
    paths += [m.group(1) for m in _TRACE.finditer(issue)] + [m.group(1) for m in _JS_TRACE.finditer(issue)]
    terms: List[str] = []
    for span in _BACKTICK.findall(issue):
        terms += re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", span)
    terms += _IDENT.findall(issue) + _ERROR.findall(issue)
    expanded: List[str] = []
    for t in terms:
        expanded.append(t)
        if "." in t:
            expanded += [p for p in t.split(".") if len(p) > 2]
    seen: Dict[str, None] = {}
    for t in expanded:
        t = t.strip("._")
        if len(t) < 3 or t.lower() in _STOP or t.isdigit():
            continue
        seen.setdefault(t, None)
    return list(seen)[:25], list(dict.fromkeys(paths))


def outline(path: Path, limit: int = 40) -> str:
    """Class and function signatures of a source file, with line numbers."""
    pat = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+\w+|^\s*(?:export\s+)?(?:async\s+)?function\s+\w+|"
                     r"^\s*(?:export\s+)?class\s+\w+|^\s*func\s+|^\s*(?:pub\s+)?fn\s+\w+|^\s*(?:public|private|protected)\s+[\w<>\[\]]+\s+\w+\s*\(")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    out = [f"{n:>5}  {l.rstrip()[:120]}" for n, l in enumerate(lines, 1) if pat.match(l)]
    if len(out) > limit:
        out = out[:limit] + [f"      ... {len(out) - limit} more"]
    return "\n".join(out)


def localize(ws: Workspace, issue: str, top: int = 8) -> str:
    terms, paths = extract_terms(issue)
    files = [f for f in ws.tracked_files() if not any(p in SKIP_DIRS for p in Path(f).parts)]
    if not files:
        return ""
    scores: Dict[str, float] = {}
    reasons: Dict[str, Set[str]] = {}

    for p in paths:
        p_norm = p.replace("\\", "/").lstrip("/")
        for f in files:
            if f.endswith(p_norm) or (len(p_norm) > 4 and p_norm.endswith(f)):
                scores[f] = scores.get(f, 0) + 6
                reasons.setdefault(f, set()).add("named in the issue")

    texts: Dict[str, str] = {}
    for f in files:
        full = ws.root / f
        try:
            if full.stat().st_size > 800_000:
                continue
            data = full.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:2048]:
            continue
        texts[f] = data.decode("utf-8", errors="replace")

    n_files = max(len(texts), 1)
    for term in terms:
        rx = re.compile(r"\b" + re.escape(term) + r"\b")
        hits = [f for f, t in texts.items() if rx.search(t)]
        if not hits or len(hits) > max(40, n_files // 3):
            continue
        weight = math.log(1 + n_files / len(hits))
        for f in hits:
            defines = re.search(r"(def|class|function|func|fn)\s+" + re.escape(term) + r"\b", texts[f])
            scores[f] = scores.get(f, 0) + weight * (2.0 if defines else 1.0)
            reasons.setdefault(f, set()).add(term)

    if not scores:
        return ""
    # The fix almost always lives in source files, so tests and docs rank lower.
    from .tools import is_test_path
    for f in scores:
        if is_test_path(f):
            scores[f] *= 0.5
        elif Path(f).suffix.lower() in (".md", ".rst", ".txt"):
            scores[f] *= 0.3
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top]
    lines = ["Likely relevant files (keyword ranking from the issue text; verify before trusting it):"]
    for f, _ in ranked:
        why = ", ".join(sorted(reasons.get(f, set()))[:6])
        lines.append(f"- {f}  [{why}]")
    source = [f for f, _ in ranked if Path(f).suffix in (".py", ".js", ".ts", ".tsx", ".mjs", ".go", ".rs", ".java")]
    for f in source[:3]:
        o = outline(ws.root / f)
        if o:
            lines.append(f"\nOutline of {f}:\n{o}")
    return "\n".join(lines)
