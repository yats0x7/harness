"""Read-only repository discovery for pre-task orientation and demos."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from .workspace import SKIP_DIRS

_CODE_EXTENSIONS = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".php", ".cs"}
_TEST_PARTS = {"test", "tests", "spec", "specs", "__tests__"}
_SIGNALS = (
    ("error", re.compile(r"\b(TODO|FIXME|XXX|HACK|NotImplementedError|NotImplemented)\b|except\s+Exception\s*:\s*pass", re.I)),
    ("structural", re.compile(r"\b(deprecated|legacy|temporary|workaround|unsafe)\b", re.I)),
)


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    line: int
    summary: str
    confidence: str


def _files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_DIRS or part.startswith(".") and part in {".git", ".hg", ".svn"}
               for part in relative.parts):
            continue
        yield path


def _read(path: Path) -> List[str]:
    try:
        if path.stat().st_size > 800_000:
            return []
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data[:4096]:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def discover_repository(repo: str, lenses: Sequence[str] = ("error", "test", "structural"), limit: int = 50) -> Dict:
    """Return bounded findings without invoking a model or changing the repo."""
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"repository path does not exist: {repo}")
    requested = set(lenses) & {"error", "test", "structural"}
    if not requested:
        raise ValueError("lenses must include error, test, or structural")
    paths = list(_files(root))
    findings: List[Finding] = []
    test_files = []
    code_files = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        parts = set(path.relative_to(root).parts)
        is_test = bool(parts & _TEST_PARTS) or path.name.startswith("test_") or ".test." in path.name
        if is_test:
            test_files.append(rel)
        if path.suffix.lower() in _CODE_EXTENSIONS:
            code_files.append(rel)
        lines = _read(path)
        if "error" in requested or "structural" in requested:
            for number, line in enumerate(lines, 1):
                for kind, pattern in _SIGNALS:
                    if kind in requested and pattern.search(line):
                        findings.append(Finding(kind, rel, number, line.strip()[:180], "medium" if kind == "error" else "low"))
                        break
                if len(findings) >= limit:
                    break
        if len(findings) >= limit:
            break

    if "test" in requested:
        for rel in sorted(test_files)[:limit]:
            findings.append(Finding("test", rel, 1, "test file available for targeted verification", "high"))
            if len(findings) >= limit:
                break
    if "structural" in requested:
        for rel in sorted(code_files, key=lambda p: p.count("/"))[:limit]:
            if not any(f.path == rel for f in findings):
                findings.append(Finding("structural", rel, 1, "source file in the repository surface", "low"))
            if len(findings) >= limit:
                break

    findings = findings[:limit]
    plan = []
    if "error" in requested:
        plan.append("Inspect error/legacy signals and trace each one to an executable behavior.")
    if test_files:
        plan.append(f"Run the most relevant tests first ({min(len(test_files), 10)} test files discovered).")
    if code_files:
        plan.append("Read the affected source path and its direct imports before editing.")
    return {"repository": str(root), "files_scanned": len(paths), "code_files": len(code_files),
            "test_files": len(test_files), "lenses": sorted(requested),
            "findings": [asdict(f) for f in findings], "verification_plan": plan}


def render(report: Dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"
