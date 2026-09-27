"""Skills: reusable know-how that anyone can add.

A skill is a folder holding a SKILL.md file:

    ---
    name: python-pytest
    description: One line saying what it helps with and when to use it.
    ---
    Instructions for the agent, in plain Markdown.

Skills are found in three places, later ones overriding earlier ones with the
same name: the harness's own `skills/`, the user's `~/.trojan/skills/`, and the
target repository's `.trojan/skills/`. The agent sees only each skill's name and
description (a few tokens each) and loads the full text with the `use_skill`
tool when it decides a skill is relevant.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .config import ROOT

USER_SKILLS = Path(os.environ.get("TROJAN_SKILLS_HOME", Path.home() / ".trojan" / "skills"))
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_SKILL_CHARS = 12000


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    source: str  # "built-in", "user" or "repo"

    def body(self) -> str:
        text = (self.path / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        _, rest = _split_frontmatter(text)
        extras = []
        for extra in sorted(self.path.glob("*.md")):
            if extra.name != "SKILL.md":
                extras.append(f"\n\n--- {extra.name} ---\n" + extra.read_text(encoding="utf-8", errors="replace"))
        full = rest.strip() + "".join(extras)
        return full if len(full) <= MAX_SKILL_CHARS else full[:MAX_SKILL_CHARS] + "\n[... skill truncated]"


def _split_frontmatter(text: str):
    meta: Dict[str, str] = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip().lower()] = value.strip().strip('"').strip("'")
            return meta, text[end + 4:]
    return meta, text


def skill_dirs(repo: Optional[Path] = None) -> List[tuple]:
    dirs = [(ROOT / "skills", "built-in"), (USER_SKILLS, "user")]
    extra = os.environ.get("TROJAN_SKILLS_PATH", "")
    dirs += [(Path(p).expanduser(), "user") for p in extra.split(os.pathsep) if p]
    if repo is not None:
        dirs.append((repo / ".trojan" / "skills", "repo"))
    return dirs


def discover(repo: Optional[Path] = None) -> Dict[str, Skill]:
    found: Dict[str, Skill] = {}
    for base, source in skill_dirs(repo):
        if not base.is_dir():
            continue
        for folder in sorted(p for p in base.iterdir() if p.is_dir()):
            skill_md = folder / "SKILL.md"
            if not skill_md.is_file():
                continue
            meta, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
            name = (meta.get("name") or folder.name).strip().lower()
            if not _NAME.match(name):
                continue
            desc = meta.get("description", "").strip() or "(no description)"
            found[name] = Skill(name, desc[:300], folder, source)
    return found


def listing(skills: Dict[str, Skill]) -> str:
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description}" for s in sorted(skills.values(), key=lambda s: s.name)]
    return ("Skills you can load with use_skill when they fit the task (each holds tested know-how):\n"
            + "\n".join(lines))


TEMPLATE = """---
name: {name}
description: One line on what this skill helps with and when the agent should use it.
---
# {title}

Write the instructions the agent should follow when this skill applies. Good skills are short
and concrete: the exact commands to run, the pitfalls to avoid, and how to check the result.

## When to use

## Steps

## Checks before finishing
"""


def create(name: str, where: Path = USER_SKILLS) -> Path:
    name = name.strip().lower()
    if not _NAME.match(name):
        raise ValueError("skill names use lowercase letters, digits and dashes, e.g. django-migrations")
    folder = where / name
    if (folder / "SKILL.md").exists():
        raise ValueError(f"a skill named {name} already exists at {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(TEMPLATE.format(name=name, title=name.replace("-", " ").capitalize()),
                                     encoding="utf-8")
    return folder / "SKILL.md"
