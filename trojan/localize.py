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


_STRING = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'")
_FENCED = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def extract_terms(issue: str) -> Tuple[List[str], List[str]]:
    """Return (identifier-like terms, path-like strings) mentioned in the issue."""
    paths = list(dict.fromkeys(m.group(0).lstrip("./") for m in _PATH.finditer(issue)))
    paths += [m.group(1) for m in _TRACE.finditer(issue)] + [m.group(1) for m in _JS_TRACE.finditer(issue)]
    # Identifiers come from code, but the values inside string literals in an
    # example ("John", "Smith") are data, and they only add noise to the search.
    code = [_STRING.sub(" ", c) for c in _FENCED.findall(issue) + _BACKTICK.findall(_FENCED.sub(" ", issue))]
    prose = _STRING.sub(" ", _FENCED.sub(" ", issue))
    terms: List[str] = []
    for span in code:
        terms += re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", span)
    terms += _IDENT.findall(prose) + _ERROR.findall(issue)
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


_PY_FROM = re.compile(r"^\s*from\s+(\.*)([\w.]*)\s+import\s+([\w, ()*]+)", re.M)
_PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+)", re.M)
_JS_IMPORT = re.compile(r"""(?:from\s+|require\(\s*|import\(\s*)['"](\.{1,2}/[^'"]+)['"]""")


def _local_imports(rel: str, text: str, files: Set[str]) -> List[str]:
    """Repository files that `rel` imports (Python and JavaScript/TypeScript)."""
    here = Path(rel).parent
    found: List[str] = []

    def add(candidate: Path) -> None:
        for c in (str(candidate) + ".py", str(candidate / "__init__.py")):
            c = c.lstrip("./")
            if c in files:
                found.append(c)
                return

    if rel.endswith(".py"):
        for dots, mod, names in _PY_FROM.findall(text):
            base = here
            for _ in range(max(len(dots) - 1, 0)):
                base = base.parent
            parts = mod.split(".") if mod else []
            target = (base if dots else Path()) / Path(*parts) if parts else base
            add(target)
            for name in re.findall(r"\w+", names):
                add(target / name)
        for mod in _PY_IMPORT.findall(text):
            add(Path(*mod.split(".")))
    elif rel.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")):
        for spec in _JS_IMPORT.findall(text):
            target = (here / spec).as_posix()
            norm = str(Path(target))
            parts = []
            for part in Path(norm).parts:
                if part == "..":
                    parts = parts[:-1]
                elif part != ".":
                    parts.append(part)
            norm = "/".join(parts)
            for c in (norm, norm + ".js", norm + ".ts", norm + ".mjs", norm + "/index.js", norm + "/index.ts"):
                if c in files:
                    found.append(c)
                    break
    return list(dict.fromkeys(found))


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
    # Bugs often sit one call deeper than the symptom the issue describes, so
    # modules imported by the best matches inherit part of their score.
    # A module everything imports (errors, constants, __init__) says little, so
    # the inherited share shrinks with the number of files importing it.
    code_files = [f for f in texts if f.endswith((".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"))]
    graph = {f: _local_imports(f, texts[f], set(texts)) for f in code_files[:3000]}
    importers: Dict[str, int] = {}
    for deps in graph.values():
        for d in deps:
            importers[d] = importers.get(d, 0) + 1
    top_now = sorted(scores.items(), key=lambda kv: -kv[1])[:6]
    for f, sc in top_now:
        for dep in graph.get(f, []):
            if dep != f and not dep.endswith("__init__.py"):
                scores[dep] = scores.get(dep, 0) + 0.6 * sc / importers.get(dep, 1)
                reasons.setdefault(dep, set()).add(f"imported by {Path(f).name}")
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
